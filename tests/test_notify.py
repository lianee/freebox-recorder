import http.client
import json
import os
import stat
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["FBXREC_FFMPEG"] = os.path.join(HERE, "fake_ffmpeg.py")
os.environ["FBXREC_FFPROBE"] = os.path.join(HERE, "fake_ffprobe.py")

from fbxrec import app, notify  # noqa: E402
from tests.test_app import Api  # noqa: E402

GOOD = {"user": "12345678", "key": "AbCdEf123456"}


class FakeFree:
    """Stands in for smsapi.free-mobile.fr: records requests, answers `status`."""
    def __init__(self):
        self.reqs, self.status = [], 200
        outer = self

        class H(BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def do_POST(self):
                body = self.rfile.read(int(self.headers.get("Content-Length", 0)))
                outer.reqs.append((self.path, self.headers.get("Content-Type"), json.loads(body)))
                self.send_response(outer.status)
                self.send_header("Content-Length", "0")
                self.end_headers()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), H)
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.url = f"http://127.0.0.1:{self.srv.server_address[1]}/sendmsg"

    def wait(self, n, timeout=5):
        end = time.monotonic() + timeout
        while len(self.reqs) < n and time.monotonic() < end:
            time.sleep(0.02)
        return self.reqs

    def close(self):
        self.srv.shutdown()
        self.srv.server_close()


class Base(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.free = FakeFree()
        self.old_api, notify.API = notify.API, self.free.url
        self.sms = notify.Sms(os.path.join(self.tmp.name, "sms.json"))

    def tearDown(self):
        notify.API = self.old_api
        self.free.close()
        self.tmp.cleanup()


class Sms(Base):
    def test_save_send(self):
        self.sms.save(**GOOD)
        self.assertEqual(stat.S_IMODE(os.stat(self.sms.path).st_mode), 0o600)
        self.sms.send("bonjour")
        path, ctype, body = self.free.reqs[0]
        self.assertEqual((path, ctype), ("/sendmsg", "application/json"))   # credentials not in the URL
        self.assertEqual(body, {"user": GOOD["user"], "pass": GOOD["key"], "msg": "bonjour"})

    def test_validation(self):
        for user, key in (("1234567", "abcdef"), ("123456789", "abcdef"), ("1234567a", "abcdef"), (12345678, "abcdef"),
                          ("12345678", "abc"), ("12345678", "abc def"), ("12345678", "a" * 65), ("12345678", None)):
            with self.assertRaises(notify.NotifyError, msg=(user, key)):
                self.sms.save(user, key)
        self.assertFalse(os.path.exists(self.sms.path))

    def test_errors_named(self):
        self.sms.save(**GOOD)
        for code, word in ((403, "refusés"), (402, "trop"), (500, "serveur")):
            self.free.status = code
            with self.assertRaisesRegex(notify.NotifyError, word):
                self.sms.send("x")
        notify.API = "http://127.0.0.1:1/sendmsg"
        with self.assertRaisesRegex(notify.NotifyError, "injoignable"):
            self.sms.send("x")

    def test_bad_file(self):
        with open(self.sms.path, "w") as f:
            f.write("{not json")
        with self.assertRaises(notify.NotifyError):
            self.sms.config()

    def test_alert_capped_and_off_when_unset(self):
        self.sms.alert("rien")            # not configured: no request
        time.sleep(0.2)
        self.assertEqual(self.free.reqs, [])
        self.sms.sent = []
        self.sms.save(**GOOD)
        for i in range(notify.DAILY_MAX + 3):
            self.sms.alert(f"a{i}")
        time.sleep(0.5)
        self.assertEqual(len(self.free.wait(notify.DAILY_MAX)), notify.DAILY_MAX)


class AlertText(unittest.TestCase):
    base = {"name": "NOPLP", "channel": "France 2", "show_start": "2026-10-20T18:40:00+02:00"}

    def test_cases(self):
        self.assertIn("échec", app.alert_text(dict(self.base, state="failed")))
        t = app.alert_text(dict(self.base, state="done", gaps=3, missing_s=12, reconnects=1))
        self.assertEqual(t, "Enregistreur TV : « NOPLP » du 20/10 18h40 (France 2) : 3 trou(s) dans l'image (12 s), 1 coupure(s) du flux.")
        self.assertIsNone(app.alert_text(dict(self.base, state="done", gaps=0, reconnects=0, stalls=0)))
        self.assertIsNone(app.alert_text(dict(self.base, state="stopped", gaps=None)))


class DaemonAlerts(Base):
    def setUp(self):
        super().setUp()
        self.d = app.Daemon(self.tmp.name, os.path.join(self.tmp.name, "out"))
        self.d.sms.save(**GOOD)

    def test_failed_recording_sends(self):
        from tests.test_recorder import occ
        rec = app.Recording(occ(30), self.d.out_root, self.d.results, on_done=self.d.on_result)
        rec.stopped.set()           # nothing recorded: state failed
        rec.start()
        rec.join(10)
        self.assertIn("échec", self.free.wait(1)[0][2]["msg"])

    def test_disk_once_a_day(self):
        os.makedirs(self.d.out_root)
        old, app.DISK_ALERT_GB = app.DISK_ALERT_GB, 10 ** 9   # any disk is "almost full"
        try:
            self.d.check_disk()
            self.d.check_disk()
        finally:
            app.DISK_ALERT_GB = old
        time.sleep(0.3)
        self.assertEqual(len(self.free.wait(1)), 1)
        self.assertIn("Go libres", self.free.reqs[0][2]["msg"])


class ApiSms(Base):
    def setUp(self):
        super().setUp()
        self.d = app.Daemon(self.tmp.name, os.path.join(self.tmp.name, "out"))
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.d, {"good.host:1"}))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        super().tearDown()

    req = Api.req

    def test_set_test_delete(self):
        st, _ = self.req("POST", "/api/settings/sms", GOOD)
        self.assertEqual(st, 200)
        self.assertIn("activées", self.free.reqs[0][2]["msg"])      # a real SMS before saving
        st, s = self.req("GET", "/api/settings")
        self.assertEqual(s["sms"], {"configured": True, "user": "12****78"})
        self.assertNotIn(GOOD["key"], json.dumps(s))
        st, _ = self.req("POST", "/api/settings/sms/test", {})
        self.assertEqual((st, len(self.free.reqs)), (200, 2))
        st, _ = self.req("DELETE", "/api/settings/sms")
        self.assertEqual(st, 200)
        self.assertFalse(os.path.exists(self.d.sms.path))
        st, _ = self.req("POST", "/api/settings/sms/test", {})
        self.assertEqual(st, 400)

    def test_refused_credentials_not_saved(self):
        self.free.status = 403
        st, r = self.req("POST", "/api/settings/sms", GOOD)
        self.assertEqual(st, 400)
        self.assertIn("refusés", r["error"])
        self.assertFalse(os.path.exists(self.d.sms.path))

    def test_bad_bodies(self):
        for body in (dict(GOOD, extra=1), {"user": "12345678"}, dict(GOOD, key=["x"]), dict(GOOD, user=12345678)):
            st, _ = self.req("POST", "/api/settings/sms", body)
            self.assertEqual(st, 400, body)
        st, _ = self.req("POST", "/api/settings/sms", raw=json.dumps(GOOD).encode(), ctype="text/plain")
        self.assertEqual(st, 400)
        st, _ = self.req("POST", "/api/settings/sms", GOOD, host="evil.example:1")
        self.assertEqual(st, 403)
        self.assertEqual(self.free.reqs, [])


if __name__ == "__main__":
    unittest.main()
