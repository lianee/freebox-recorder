import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["FBXREC_FFMPEG"] = os.path.join(HERE, "fake_ffmpeg.py")
os.environ["FBXREC_FFPROBE"] = os.path.join(HERE, "fake_ffprobe.py")

from fbxrec import app  # noqa: E402
from fbxrec.schedule import TZ  # noqa: E402

PLAYLIST = """#EXTM3U
#EXTINF:0,2 - France 2 (auto)
rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service=201
#EXTINF:0,2 - France 2 (HD)
rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service=201&flavour=hd
#EXTINF:0,2 - France 2 (standard)
rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service=201&flavour=sd
#EXTINF:0,1 - TF1 (HD)
rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service=612&flavour=hd
"""

NOPLP = {"name": "NOPLP", "service": 201, "channel": "France 2", "flavour": "sd", "kind": "weekly",
         "days": [0, 1, 2, 3, 4, 5], "start": "18:40", "end": "20:00",
         "margin_before": 5, "margin_after": 2, "enabled": True}


class Playlist(unittest.TestCase):
    def test_parse(self):
        ch = app.parse_playlist(PLAYLIST)
        self.assertEqual([(c["number"], c["name"], c["service"]) for c in ch],
                         [(1, "TF1", 612), (2, "France 2", 201)])
        self.assertEqual(ch[1]["flavours"], ["hd", "sd"])


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.d = app.Daemon(cls.tmp.name, os.path.join(cls.tmp.name, "out"))
        cls.d.channels = app.parse_playlist(PLAYLIST)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(cls.d, {"good.host:1"}))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, host="good.host:1", ctype="application/json", raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Host": host}
        if data is not None:
            h["Content-Type"] = ctype
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        try:
            return r.status, json.loads(out)
        except ValueError:
            return r.status, out

    # positive path: create, read back, edit, toggle, delete
    def test_crud(self):
        st, s = self.req("POST", "/api/schedules", NOPLP)
        self.assertEqual(st, 200)
        st, items = self.req("GET", "/api/schedules")
        self.assertIn(s["id"], [x["id"] for x in items])
        st, s2 = self.req("POST", "/api/schedules", dict(s, end="20:05"))
        self.assertEqual((st, s2["id"]), (200, s["id"]))
        with open(os.path.join(self.tmp.name, "schedules.json")) as f:
            stored = [x for x in json.load(f) if x["id"] == s["id"]]
        self.assertEqual([x["end"] for x in stored], ["20:05"])      # replaced, not duplicated
        st, up = self.req("GET", "/api/upcoming")
        self.assertTrue(any(o["name"] == "NOPLP" for o in up))
        st, r = self.req("DELETE", "/api/schedules/" + s["id"])
        self.assertEqual((st, r["deleted"]), (200, True))
        st, items = self.req("GET", "/api/schedules")
        self.assertNotIn(s["id"], [x["id"] for x in items])

    def test_index_and_channels(self):
        st, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Enregistreur TV", body)
        st, ch = self.req("GET", "/api/channels")
        self.assertEqual(len(ch), 2)

    # attacks
    def test_foreign_host_refused(self):
        for m, p in (("GET", "/api/schedules"), ("POST", "/api/schedules"), ("DELETE", "/api/schedules/" + "a" * 12)):
            st, _ = self.req(m, p, NOPLP if m == "POST" else None, host="evil.example")
            self.assertEqual(st, 403, (m, p))
        st, _ = self.req("GET", "/", host="")
        self.assertEqual(st, 403)
        # right address, wrong port; name that only starts like an allowed one
        for h in (f"127.0.0.1:{self.port + 1}", f"127.0.0.1:{self.port}.evil.example", "127.0.0.1"):
            st, _ = self.req("GET", "/api/schedules", host=h)
            self.assertEqual(st, 403, h)

    def test_own_addresses_allowed(self):
        # no IP configured anywhere: the address the connection reached, the VM name, localhost
        for h in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"{socket.gethostname()}:{self.port}",
                  f"LOCALHOST:{self.port}", "GOOD.HOST:1"):
            st, _ = self.req("GET", "/api/schedules", host=h)
            self.assertEqual(st, 200, h)

    def test_simple_cross_site_post_refused(self):
        # what a hostile web page can send without a CORS preflight
        for ct in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", "application/json-x"):
            st, _ = self.req("POST", "/api/schedules", NOPLP, ctype=ct)
            self.assertEqual(st, 400, ct)

    def test_malformed_bodies(self):
        self.assertEqual(self.req("POST", "/api/schedules", [NOPLP])[0], 400)
        self.assertEqual(self.req("POST", "/api/schedules", raw=b"{not json")[0], 400)
        self.assertEqual(self.req("POST", "/api/schedules", raw=b"x" * (app.MAX_BODY + 1))[0], 400)
        self.assertEqual(self.req("POST", "/api/schedules", raw=b"{}", headers={"Content-Length": "abc"})[0], 400)
        self.assertEqual(self.req("POST", "/api/schedules", dict(NOPLP, name="../../etc"))[0], 400)
        self.assertEqual(self.req("POST", "/api/running/stop", ["x"])[0], 400)
        self.assertEqual(self.req("POST", "/api/running/stop", {"key": ["x"]})[0], 404)

    def test_delete_traversal(self):
        for p in ("/api/schedules/../schedules.json", "/api/schedules/%2e%2e", "/api/schedules/" + "a" * 13):
            self.assertEqual(self.req("DELETE", p)[0], 404, p)

    def test_server_survives(self):
        self.req("POST", "/api/schedules", raw=b"\xff\xfe")
        self.assertEqual(self.req("GET", "/api/schedules")[0], 200)


class DaemonStep(unittest.TestCase):
    def test_starts_once_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(TZ)
            d = app.Daemon(tmp, os.path.join(tmp, "out"))
            s = dict(NOPLP, kind="once", date=now.date().isoformat(), margin_before=0, margin_after=0,
                     start=(now - timedelta(minutes=1)).strftime("%H:%M"),
                     end=(now + timedelta(minutes=1)).strftime("%H:%M"))
            s.pop("days")
            if s["end"] < s["start"]:
                self.skipTest("too close to midnight")
            from fbxrec import schedule as sch
            d.store.put(sch.validate(s))
            d.step()
            d.step()
            self.assertEqual(len(d.running), 1)          # not started twice
            rec = next(iter(d.running.values()))
            # stop once data has arrived: a fixed 1 s sleep lost the race against a slow
            # ffmpeg start on a loaded machine (state "failed", nothing recorded, 2026-10-20)
            part, deadline = rec.base + ".part0.ts", time.monotonic() + 15
            while not (os.path.exists(part) and os.path.getsize(part)) and time.monotonic() < deadline:
                time.sleep(0.05)
            rec.stop()
            rec.join(10)
            with open(d.results) as f:
                info = json.loads(f.readline())
            self.assertEqual(info["state"], "stopped")
            self.assertTrue(os.path.getsize(info["file"]) > 0)


if __name__ == "__main__":
    unittest.main()
