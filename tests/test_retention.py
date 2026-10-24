import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["FBXREC_FFMPEG"] = os.path.join(HERE, "fake_ffmpeg.py")
os.environ["FBXREC_FFPROBE"] = os.path.join(HERE, "fake_ffprobe.py")

from fbxrec import app, retention  # noqa: E402
from fbxrec.schedule import TZ  # noqa: E402
from tests.test_app import Api  # noqa: E402

NOW = datetime(2026, 10, 20, 21, 0, tzinfo=TZ)


def put(path, data=b"V", mtime=None):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    if mtime:
        os.utime(path, (mtime, mtime))
    return path


class Rules(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out")
        n = os.path.join(self.out, "NOPLP")
        self.old = put(os.path.join(n, "2026-10-01 18h40 - France 2.mkv"), mtime=NOW.timestamp())   # converted today
        self.new = put(os.path.join(n, "2026-10-19 18h40 - France 2.mkv"))
        self.part = put(os.path.join(n, "2026-09-01 18h40 - France 2.part0.ts"))
        self.result = put(os.path.join(n, "2026-10-01 18h40 - France 2", "noplp.json"), b"{}")   # tv-extract
        self.note = put(os.path.join(n, "notes.txt"))
        self.kept = put(os.path.join(self.out, "_morceaux", "2020-01-01 10h00 - x.ts"))
        self.r = retention.Retention(os.path.join(self.tmp.name, "retention.json"), self.out)

    def tearDown(self):
        self.tmp.cleanup()

    def test_never_by_default(self):
        self.assertEqual(self.r.apply(NOW), [])
        self.assertEqual(self.r.summary(), [{"folder": "NOPLP", "files": 2, "size": 2, "days": None}])

    def test_deletes_by_show_date_only_videos(self):
        self.r.set("NOPLP", 10)
        self.assertEqual(self.r.apply(NOW), [self.old])
        for p in (self.new, self.part, self.result, self.note, self.kept):
            self.assertTrue(os.path.exists(p), p)

    def test_running_recording_spared(self):
        self.r.set("NOPLP", 10)
        self.assertEqual(self.r.apply(NOW, {self.old[:-4]}), [])

    def test_unnamed_file_uses_mtime(self):
        odd = put(os.path.join(self.out, "Immédiat", "film.mkv"), mtime=(NOW - timedelta(days=40)).timestamp())
        self.r.set("Immédiat", 30)
        self.assertEqual(self.r.apply(NOW), [odd])

    def test_set_validation_and_unset(self):
        for folder, days in (("NOPLP", 0), ("NOPLP", 3651), ("NOPLP", "10"), ("NOPLP", True), ("NOPLP", 1.5),
                             ("_morceaux", 5), ("..", 5), ("../out", 5), ("Absent", 5), (None, 5)):
            with self.assertRaises(retention.RetentionError, msg=(folder, days)):
                self.r.set(folder, days)
        self.r.set("NOPLP", 30)
        self.r.set("NOPLP", None)
        self.assertEqual(self.r.rules(), {})


class ApiRetention(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out")
        put(os.path.join(self.out, "NOPLP", "2026-10-19 18h40 - France 2.mkv"))
        self.d = app.Daemon(self.tmp.name, self.out)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.d, {"good.host:1"}))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    req = Api.req

    def test_roundtrip_and_rejects(self):
        st, rows = self.req("GET", "/api/retention")
        self.assertEqual((st, rows[0]["folder"], rows[0]["days"]), (200, "NOPLP", None))
        st, _ = self.req("POST", "/api/retention", {"folder": "NOPLP", "days": 30})
        self.assertEqual(st, 200)
        self.assertEqual(self.req("GET", "/api/retention")[1][0]["days"], 30)
        for body in ({"folder": "NOPLP"}, {"folder": "NOPLP", "days": 30, "x": 1}, {"folder": "..", "days": 3},
                     {"folder": "NOPLP", "days": -1}):
            st, _ = self.req("POST", "/api/retention", body)
            self.assertEqual(st, 400, body)
        st, _ = self.req("POST", "/api/retention", raw=b'{"folder":"NOPLP","days":1}', ctype="text/plain")
        self.assertEqual(st, 400)
        st, _ = self.req("POST", "/api/retention", {"folder": "NOPLP", "days": 1}, host="evil.example:1")
        self.assertEqual(st, 403)
        self.assertEqual(self.d.retention.rules(), {"NOPLP": 30})


if __name__ == "__main__":
    unittest.main()
