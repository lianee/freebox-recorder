import base64
import http.client
import io
import json
import os
import tempfile
import threading
import unittest
import urllib.error
from http.server import ThreadingHTTPServer
from datetime import timedelta
from types import SimpleNamespace

from fbxrec import __version__, app, auth, update
from fbxrec import schedule as sch


def basic(pw):
    return "Basic " + base64.b64encode(f"x:{pw}".encode()).decode()


class Release(unittest.TestCase):
    def opener(self, tag=None, code=None):
        def op(url, timeout):
            if code:
                raise urllib.error.HTTPError(url, code, "x", {}, None)
            return io.BytesIO(json.dumps({"tag_name": tag}).encode())
        return op

    def test_versions(self):
        self.assertEqual(update.latest("a/b", self.opener(code=404))["latest"], None)
        self.assertTrue(update.latest("a/b", self.opener("v99.0.0"))["newer"])
        self.assertFalse(update.latest("a/b", self.opener("v" + __version__))["newer"])
        self.assertFalse(update.latest("a/b", self.opener("0.0.1"))["newer"])
        with self.assertRaises(ValueError):
            update.latest("a/b", self.opener("latest"))
        with self.assertRaises(urllib.error.HTTPError):
            update.latest("a/b", self.opener(code=500))


class FakeRun:
    def __init__(self, rc=0, err=""):
        self.calls, self.rc, self.err = [], rc, err

    def __call__(self, cmd, **kw):
        self.calls.append(cmd)
        return SimpleNamespace(returncode=self.rc, stderr=self.err, stdout="")


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pw = os.path.join(cls.tmp.name, "password")
        os.makedirs(os.path.join(cls.tmp.name, "out"))
        cls.d = app.Daemon(cls.tmp.name, os.path.join(cls.tmp.name, "out"))
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(cls.d, {"h:1"}, auth.Guard(cls.pw)))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        auth.remove_password(self.pw)
        self.d.running.clear()
        self.real_run, update.run = update.run, FakeRun()

    def tearDown(self):
        update.run = self.real_run

    def req(self, method, path, body=None, authz=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        h = {"Host": "h:1"}
        if body is not None:
            h["Content-Type"] = "application/json"
        if authz:
            h["Authorization"] = authz
        c.request(method, path, body=json.dumps(body) if body is not None else None, headers=h)
        r = c.getresponse()
        data = r.read()
        c.close()
        return r.status, json.loads(data) if data else None

    def test_settings(self):
        st, s = self.req("GET", "/api/settings")
        self.assertEqual(st, 200)
        self.assertEqual((s["version"], s["password"], s["update_allowed"]), (__version__, False, True))
        self.assertGreater(s["disk"]["total"], 0)
        self.assertEqual(s["repo"], "lianee/freebox-recorder")

    def test_password_lifecycle(self):
        for bad in ("court", 12345678, None, ["a" * 10], "x" * 201):
            self.assertEqual(self.req("POST", "/api/settings/password", {"password": bad})[0], 400, repr(bad))
        self.assertFalse(os.path.exists(self.pw))
        self.assertEqual(self.req("POST", "/api/settings/password", {"password": "premier mdp"})[0], 200)
        with open(self.pw) as f:
            self.assertTrue(auth.check("premier mdp", f.read().strip()))
        # now protected: changing or removing needs the current password
        self.assertEqual(self.req("POST", "/api/settings/password", {"password": "pirate123"})[0], 401)
        self.assertEqual(self.req("DELETE", "/api/settings/password")[0], 401)
        self.assertEqual(self.req("POST", "/api/settings/password", {"password": "second mdp"}, basic("premier mdp"))[0], 200)
        self.assertEqual(self.req("GET", "/api/settings", authz=basic("premier mdp"))[0], 401)
        self.assertTrue(self.req("GET", "/api/settings", authz=basic("second mdp"))[1]["password"])
        self.assertEqual(self.req("DELETE", "/api/settings/password", authz=basic("second mdp"))[0], 200)
        self.assertFalse(os.path.exists(self.pw))

    def test_update_starts_unit(self):
        st, _ = self.req("POST", "/api/update", {})
        self.assertEqual(st, 202)
        cmd = update.run.calls[-1]
        self.assertEqual(cmd[:3], ["sudo", "-n", "systemd-run"])
        self.assertIn("/opt/fbxrec/install.sh --update", " ".join(cmd))

    def test_update_refused_during_recording(self):
        self.d.running["k"] = SimpleNamespace(is_alive=lambda: True)
        self.assertEqual(self.req("POST", "/api/update", {})[0], 409)
        self.assertEqual(update.run.calls, [])

    def test_update_already_running(self):
        update.run = FakeRun(1, "Failed to start transient service unit: Unit fbxrec-update.service already exists.")
        st, r = self.req("POST", "/api/update", {})
        self.assertEqual((st, r["error"]), (409, "une mise à jour est déjà en cours"))

    def test_update_needs_json(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/api/update", body="x", headers={"Host": "h:1", "Content-Type": "text/plain"})
        self.assertEqual(c.getresponse().status, 400)
        c.close()
        self.assertEqual(update.run.calls, [])

    def test_reboot_flag(self):
        flag = os.path.join(self.tmp.name, "reboot-required")
        real, update.REBOOT_FLAG = update.REBOOT_FLAG, flag
        try:
            self.assertIsNone(self.req("GET", "/api/settings")[1]["reboot_required"])
            open(flag, "w").close()
            self.assertEqual(self.req("GET", "/api/settings")[1]["reboot_required"], [])
            with open(flag + ".pkgs", "w") as f:
                f.write("linux-image-6.1.0-55-arm64\n")
            self.assertEqual(self.req("GET", "/api/settings")[1]["reboot_required"], ["linux-image-6.1.0-55-arm64"])
        finally:
            update.REBOOT_FLAG = real

    def test_reboot(self):
        self.assertEqual(self.req("POST", "/api/reboot", {})[0], 202)
        self.assertEqual(update.run.calls[-1], ["sudo", "-n", "systemctl", "reboot"])

    def test_reboot_refused_during_recording(self):
        self.d.running["k"] = SimpleNamespace(is_alive=lambda: True)
        self.assertEqual(self.req("POST", "/api/reboot", {})[0], 409)
        self.assertEqual(update.run.calls, [])

    def test_reboot_refused_before_a_recording(self):
        now = self.d.now()
        soon = now + timedelta(minutes=5)
        s = sch.validate({"name": "Bientot", "service": 201, "channel": "France 2", "flavour": "sd",
                          "kind": "once", "date": soon.date().isoformat(), "start": soon.strftime("%H:%M"),
                          "end": (soon + timedelta(minutes=30)).strftime("%H:%M"),
                          "margin_before": 0, "margin_after": 0, "enabled": True})
        self.d.store.put(s)
        try:
            st, r = self.req("POST", "/api/reboot", {})
            self.assertEqual(st, 409)
            self.assertIn("Bientot", r["error"])
            self.assertEqual(update.run.calls, [])
        finally:
            self.d.store.delete(s["id"])

    def test_reboot_needs_json_and_auth(self):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=10)
        c.request("POST", "/api/reboot", body="{}", headers={"Host": "h:1", "Content-Type": "text/plain"})
        self.assertEqual(c.getresponse().status, 400)
        c.close()
        auth.write_password(self.pw, "un mot de passe")
        self.assertEqual(self.req("POST", "/api/reboot", {})[0], 401)
        self.assertEqual(update.run.calls, [])

    def test_update_not_allowed(self):
        update.run = FakeRun(1)
        self.assertFalse(self.req("GET", "/api/settings")[1]["update_allowed"])


if __name__ == "__main__":
    unittest.main()
