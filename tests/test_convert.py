import json
import os
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["FBXREC_FFMPEG"] = os.path.join(HERE, "fake_ffmpeg.py")
os.environ["FBXREC_FFPROBE"] = os.path.join(HERE, "fake_ffprobe.py")

from fbxrec import app, convert  # noqa: E402
from fbxrec import schedule as sch  # noqa: E402


def touch(path, data=b"TS", age=600):
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(data)
    t = time.time() - age
    os.utime(path, (t, t))
    return path


class Convert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = self.tmp.name
        self.ts = touch(os.path.join(self.out, "NOPLP", "2026-10-19 18h40 - France 2.ts"))
        self.log = os.path.join(self.out, "cmd.log")
        os.environ["FAKE_LOG"] = self.log
        for k in ("FAKE_CONVERT", "FAKE_MKV_DURATION"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()

    def files(self):
        return sorted(os.listdir(os.path.join(self.out, "NOPLP")))

    def test_converts_and_replaces(self):
        mkv = convert.convert(self.ts)
        self.assertEqual(self.files(), ["2026-10-19 18h40 - France 2.mkv"])
        self.assertEqual(mkv, self.ts[:-3] + ".mkv")
        with open(self.log) as f:
            cmd = [json.loads(l) for l in f if "matroska" in l][-1]
        self.assertEqual(cmd[cmd.index("-field_order") + 1], "progressive")   # Showtime crash otherwise
        self.assertEqual(cmd[cmd.index("-c:v") + 1], "copy")
        self.assertEqual(cmd[cmd.index("-c:a") + 1], "aac")
        self.assertIn("noise=drop=eq(pts\\,nopts)", cmd)

    def test_duration_mismatch_keeps_ts(self):
        os.environ["FAKE_MKV_DURATION"] = "128.9"   # ffmpeg 5.1 stopping at a damaged packet
        with self.assertRaises(convert.ConvertError):
            convert.convert(self.ts)
        self.assertEqual(self.files(), ["2026-10-19 18h40 - France 2.ts"])

    def test_ffmpeg_failure_keeps_ts(self):
        os.environ["FAKE_CONVERT"] = "fail"
        with self.assertRaises(convert.ConvertError):
            convert.convert(self.ts)
        self.assertEqual(self.files(), ["2026-10-19 18h40 - France 2.ts"])

    def test_interrupted(self):
        os.environ["FAKE_CONVERT"] = "slow"
        t0 = time.monotonic()
        with self.assertRaisesRegex(convert.ConvertError, "interrupted"):
            convert.convert(self.ts, should_stop=lambda: time.monotonic() - t0 > 0.5, poll_s=0.2)
        self.assertLess(time.monotonic() - t0, 5)
        self.assertEqual(self.files(), ["2026-10-19 18h40 - France 2.ts"])

    def test_leftover_ts_beside_verified_mkv(self):
        touch(self.ts[:-3] + ".mkv", b"MKV")
        convert.convert(self.ts)
        self.assertEqual(self.files(), ["2026-10-19 18h40 - France 2.mkv"])

    def test_candidates(self):
        new = touch(os.path.join(self.out, "Immédiat", "new.ts"), age=200)
        touch(os.path.join(self.out, "Immédiat", "growing.ts"), age=10)
        touch(os.path.join(self.out, "Immédiat", "x.part0.ts"))
        touch(os.path.join(self.out, "_morceaux", "kept.ts"))
        touch(os.path.join(self.out, ".fbxprivrec", "pvr.ts"))
        busy = touch(os.path.join(self.out, "Immédiat", "busy.ts"))
        touch(os.path.join(self.out, "Immédiat", "done.mkv"))
        self.assertEqual(convert.candidates(self.out, {busy[:-3]}), [new, self.ts])


class DaemonConvert(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out")
        self.ts = touch(os.path.join(self.out, "NOPLP", "a.ts"))
        self.d = app.Daemon(self.tmp.name, self.out)
        os.environ["FAKE_LOG"] = os.path.join(self.tmp.name, "cmd.log")   # the previous test's dir is gone
        for k in ("FAKE_CONVERT", "FAKE_MKV_DURATION"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()

    def test_idle_converts(self):
        self.assertTrue(self.d.convert_step())
        self.assertTrue(os.path.exists(self.ts[:-3] + ".mkv"))

    def test_waits_while_recording(self):
        class Alive:
            base = "x"
            def is_alive(self): return True
        self.d.running["k"] = Alive()
        self.assertFalse(self.d.convert_step())
        self.assertTrue(os.path.exists(self.ts))

    def test_waits_before_a_recording(self):
        base = (datetime.now(sch.TZ) + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        self.d.now = lambda: base          # no midnight to skip
        now = base + timedelta(minutes=10)
        self.d.store.put(sch.validate({"name": "Soon", "service": 201, "channel": "France 2", "flavour": "sd",
                                       "kind": "once", "date": now.date().isoformat(),
                                       "start": now.strftime("%H:%M"),
                                       "end": (now + timedelta(minutes=30)).strftime("%H:%M"),
                                       "margin_before": 0, "margin_after": 0, "enabled": True}))
        self.assertFalse(self.d.convert_step())
        self.assertTrue(os.path.exists(self.ts))

    def test_failure_not_retried(self):
        os.environ["FAKE_CONVERT"] = "fail"
        self.assertFalse(self.d.convert_step())
        os.environ.pop("FAKE_CONVERT")
        self.assertFalse(self.d.convert_step())   # same file skipped until a restart
        self.assertTrue(os.path.exists(self.ts))

    def test_results_follow_the_conversion(self):
        with open(self.d.results, "w") as f:
            f.write(json.dumps({"key": "k", "file": self.ts}) + "\n")
        self.d.convert_step()
        self.assertEqual(self.d.recent_results()[0]["file"], self.ts[:-3] + ".mkv")


if __name__ == "__main__":
    unittest.main()
