import base64
import http.client
import os
import tempfile
import threading
import time
import unittest
from http.server import ThreadingHTTPServer

from fbxrec import app, auth


def basic(user, pw):
    return "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()


def write(path, text):
    with open(path, "w") as f:
        f.write(text)
    # mtime granularity: make every rewrite visible to the reload check
    t = time.time() + write.n
    write.n += 1
    os.utime(path, (t, t))
write.n = 0


class Hash(unittest.TestCase):
    def test_roundtrip(self):
        h = auth.make_hash("correct horse", iterations=1000)
        self.assertTrue(auth.check("correct horse", h))
        self.assertFalse(auth.check("correct hors", h))
        self.assertFalse(auth.check("", h))

    def test_salted(self):
        self.assertNotEqual(auth.make_hash("x" * 8, iterations=1000), auth.make_hash("x" * 8, iterations=1000))


class Prompt(unittest.TestCase):
    def run_prompt(self, answers):
        it, said = iter(answers), []
        return auth.ask_password(lambda _: next(it), said.append), said

    def test_retries_until_valid(self):
        pw, said = self.run_prompt(["court", "assez long", "pas pareil", "assez long", "assez long"])
        self.assertEqual(pw, "assez long")
        self.assertEqual(len(said), 2)          # one "too short", one "mismatch"

    def test_empty_cancels(self):
        self.assertEqual(self.run_prompt(["court", ""])[0], None)


class GuardFile(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.tmp.name, "password")
        self.g = auth.Guard(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_no_file_open(self):
        self.assertFalse(self.g.enabled())
        self.assertTrue(self.g.allows(None))

    def test_set_change_remove_without_restart(self):
        write(self.path, auth.make_hash("premier mdp", iterations=1000))
        self.assertFalse(self.g.allows(None))
        self.assertTrue(self.g.allows(basic("x", "premier mdp")))
        write(self.path, auth.make_hash("second mdp", iterations=1000))
        self.assertFalse(self.g.allows(basic("x", "premier mdp")), "old password still cached")
        self.assertTrue(self.g.allows(basic("y", "second mdp")))
        os.remove(self.path)
        self.assertTrue(self.g.allows(None))

    def test_malformed_or_empty_fails_closed(self):
        for text in ("", "\n", "plaintext", "md5$1$ab$cd", "pbkdf2_sha256$1$zz$" + "0" * 64):
            write(self.path, text)
            with self.assertRaises(ValueError, msg=repr(text)):
                self.g.allows(basic("x", "anything"))

    def test_bad_headers(self):
        write(self.path, auth.make_hash("le bon mdp", iterations=1000))
        for h in ("", "Basic", "Basic !!!", "Bearer abc", "basic " + basic("x", "le bon mdp")[6:],
                  "Basic " + base64.b64encode(b"\xff\xfe:x").decode(),
                  "Basic " + base64.b64encode(b"le bon mdp").decode()):   # no colon: whole string is the user
            self.assertFalse(self.g.allows(h), repr(h))
        self.assertTrue(self.g.allows(basic("", "le bon mdp")))
        self.assertTrue(self.g.allows(basic("a:b", "le bon mdp")) is False)   # user cannot contain ':'


class ApiWithPassword(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.pw = os.path.join(cls.tmp.name, "password")
        cls.d = app.Daemon(cls.tmp.name, os.path.join(cls.tmp.name, "out"))
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0),
                                      app.make_handler(cls.d, {"good.host:1"}, auth.Guard(cls.pw)))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def setUp(self):
        write(self.pw, auth.make_hash("mot de passe", iterations=1000))

    def get(self, path, authz=None, host="good.host:1", method="GET"):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        h = {"Host": host}
        if authz:
            h["Authorization"] = authz
        c.request(method, path, headers=h)
        r = c.getresponse()
        r.read()
        c.close()
        return r.status, r.getheader("WWW-Authenticate")

    def test_challenge_then_access(self):
        self.assertEqual(self.get("/")[0], 401)
        self.assertIn("Basic", self.get("/api/schedules")[1])
        self.assertEqual(self.get("/", basic("moi", "mot de passe"))[0], 200)
        self.assertEqual(self.get("/api/schedules", basic("moi", "mauvais"))[0], 401)
        self.assertEqual(self.get("/api/schedules/0123456789ab", None, method="DELETE")[0], 401)

    def test_instant_endpoints_need_auth(self):
        for path in ("/api/instant", "/api/running/end", "/api/running/stop", "/api/settings/sms",
                     "/api/settings/sms/test", "/api/retention", "/api/favorites"):
            self.assertEqual(self.get(path, method="POST")[0], 401, path)
        for path in ("/api/play?file=a.ts&t=0", "/api/probe?file=a.ts", "/api/epg?start=1", "/api/file?file=a.mkv", "/api/retention"):
            self.assertEqual(self.get(path)[0], 401, path)
            self.assertEqual(self.get(path, basic("moi", "mauvais"), method="POST")[0], 401, path)
        self.assertEqual(self.get("/api/settings/sms", method="DELETE")[0], 401)
        self.assertEqual(self.get("/api/recording?file=a.mkv", method="DELETE")[0], 401)
        self.assertEqual(self.d.running, {})

    def test_running_open_to_loopback_only(self):
        # the test client is on 127.0.0.1: /api/running answers, everything else still asks
        self.assertEqual(self.get("/api/running")[0], 200)
        self.assertEqual(self.get("/api/running?x")[0], 401)
        self.assertEqual(self.get("/api/busy?within=10")[0], 200)    # install.sh asks it too
        self.assertEqual(self.get("/api/busy?within=10&x=1")[0], 401)
        self.assertEqual(self.get("/api/busy")[0], 401)
        self.assertEqual(self.get("/api/results")[0], 401)
        self.assertEqual(self.get("/api/running", method="POST")[0], 401)

    def test_host_check_still_first(self):
        self.assertEqual(self.get("/", basic("moi", "mot de passe"), host="evil.example:1")[0], 403)

    def test_malformed_file_refuses(self):
        write(self.pw, "garbage")
        self.assertEqual(self.get("/", basic("moi", "mot de passe"))[0], 500)

    def test_removed_file_opens(self):
        os.remove(self.pw)
        self.assertEqual(self.get("/api/schedules")[0], 200)


if __name__ == "__main__":
    unittest.main()
