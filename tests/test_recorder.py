import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["FBXREC_FFMPEG"] = os.path.join(HERE, "fake_ffmpeg.py")
os.environ["FBXREC_FFPROBE"] = os.path.join(HERE, "fake_ffprobe.py")
os.environ["FBXREC_RETRY_DELAY"] = "0.1"

from fbxrec import recorder  # noqa: E402
from fbxrec.recorder import Recording  # noqa: E402

recorder.RETRY_DELAY = 0.1   # the module may already be imported by another test file
from fbxrec.schedule import TZ, Occurrence  # noqa: E402


def occ(seconds, name="NOPLP", channel="France 2"):
    now = datetime.now(TZ)
    return Occurrence("a" * 12, name, 201, "sd", now, now + timedelta(seconds=seconds), now, channel)


class RecorderTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out")
        self.res = os.path.join(self.tmp.name, "results.jsonl")
        self.log = os.path.join(self.tmp.name, "cmd.log")
        os.environ["FAKE_LOG"] = self.log
        for k in ("FAKE_RUN", "FAKE_NOFRA", "FAKE_STALL_ONCE", "FAKE_GROW"):
            os.environ.pop(k, None)

    def tearDown(self):
        self.tmp.cleanup()

    def run_rec(self, o):
        r = Recording(o, self.out, self.res)
        r.start()
        r.join(30)
        self.assertFalse(r.is_alive())
        with open(self.res) as f:
            return json.loads(f.readlines()[-1])

    def cmds(self):
        with open(self.log) as f:
            return [json.loads(l) for l in f if "-map" in l]

    def test_single_segment(self):
        info = self.run_rec(occ(3))
        self.assertEqual((info["state"], info["segments"], info["reconnects"]), ("done", 1, 0))
        self.assertTrue(os.path.exists(info["file"]))
        self.assertEqual(info["gaps"], 1)
        self.assertEqual(info["missing_s"], 5)
        self.assertIn("0:a:m:language:fra", self.cmds()[0])
        self.assertIn("0:v:0", self.cmds()[0])
        self.assertEqual(os.listdir(os.path.join(self.out, "NOPLP")), [os.path.basename(info["file"])])
        self.assertRegex(os.path.basename(info["file"]), r"^\d{4}-\d{2}-\d{2} \d{2}h\d{2} - France 2\.ts$")

    def test_hung_ffmpeg_is_killed_and_retried(self):
        # 2026-10-18: Freebox disk share stalled, RTSP input died, ffmpeg hung forever
        # (ignoring SIGTERM): no retry, the recording never ended
        os.environ["FAKE_STALL_ONCE"] = os.path.join(self.tmp.name, "stalled")
        os.environ["FAKE_GROW"] = "1"     # the retry gets a live stream: it must not be killed
        old, recorder.STALL_TIMEOUT = recorder.STALL_TIMEOUT, 1
        try:
            info = self.run_rec(occ(5))
        finally:
            recorder.STALL_TIMEOUT = old
        self.assertEqual(info["state"], "done")
        self.assertEqual(info["stalls"], 1)
        self.assertGreaterEqual(info["reconnects"], 1)
        self.assertEqual(info["segments"], 2)

    def test_end_during_retry_delay_is_not_a_reconnect(self):
        # the slot ends while waiting to retry: no new capture, so no reconnect either
        # (seen as a flaky segments != reconnects + 1, mostly on the slower VM)
        os.environ["FAKE_RUN"] = "0.8"
        old, recorder.RETRY_DELAY = recorder.RETRY_DELAY, 1
        try:
            info = self.run_rec(occ(3))
        finally:
            recorder.RETRY_DELAY = old
        self.assertEqual(len(self.cmds()), 1)
        self.assertEqual((info["segments"], info["reconnects"]), (1, 0))

    def test_slash_in_channel_name(self):
        info = self.run_rec(occ(3, channel="TeleBom/TeleDom"))
        self.assertEqual(os.path.dirname(info["file"]), os.path.join(self.out, "NOPLP"))
        self.assertTrue(info["file"].endswith(" - TeleBom-TeleDom.ts"))

    def test_reconnect_and_concat(self):
        os.environ["FAKE_RUN"] = "1"      # stream drops every second
        info = self.run_rec(occ(5.5))
        self.assertGreaterEqual(info["reconnects"], 2)
        self.assertEqual(info["segments"], info["reconnects"] + 1)
        self.assertEqual(os.path.getsize(info["file"]), 188 * info["segments"])
        self.assertEqual(info["missed"], 2 * info["segments"])
        # no fixed duration: the end may move, the capture loop stops ffmpeg itself
        self.assertFalse([c for c in self.cmds() if "-t" in c])
        self.assertFalse([f for f in os.listdir(os.path.join(self.out, "NOPLP")) if ".part" in f])

    def test_end_moved_later_keeps_one_capture(self):
        os.environ["FAKE_GROW"] = "1"
        r = Recording(occ(2.5), self.out, self.res)
        t0 = time.monotonic()
        r.start()
        time.sleep(0.5)
        r.set_end(datetime.now(TZ) + timedelta(seconds=4))
        r.join(30)
        elapsed = time.monotonic() - t0
        with open(self.res) as f:
            info = json.loads(f.readlines()[-1])
        self.assertEqual((info["state"], info["segments"], info["reconnects"]), ("done", 1, 0))
        self.assertGreater(elapsed, 4)
        self.assertEqual(len(self.cmds()), 1)

    def test_end_moved_earlier_stops_soon(self):
        os.environ["FAKE_GROW"] = "1"
        r = Recording(occ(60), self.out, self.res)
        t0 = time.monotonic()
        r.start()
        time.sleep(0.5)
        r.set_end(datetime.now(TZ) + timedelta(seconds=1))
        r.join(30)
        self.assertLess(time.monotonic() - t0, 8)   # the capture loop polls every 5 s at most
        with open(self.res) as f:
            info = json.loads(f.readlines()[-1])
        self.assertEqual((info["state"], info["segments"]), ("done", 1))

    def test_failed_leaves_no_empty_part(self):
        r = Recording(occ(30), self.out, self.res)
        r.stopped.set()      # stops before any capture: nothing recorded
        r.start(); r.join(10)
        with open(self.res) as f:
            self.assertEqual(json.loads(f.readlines()[-1])["state"], "failed")
        self.assertEqual(os.listdir(os.path.join(self.out, "NOPLP")), [])

    def test_same_name_same_second_gets_two_files(self):
        # 2026-10-20 live: two instant recordings of one channel in the same second shared a
        # name, both ffmpeg wrote the same .part0.ts. Both are built before either runs.
        os.environ["FAKE_GROW"] = "1"
        o = occ(3)
        a, b = Recording(o, self.out, self.res), Recording(o, self.out, self.res)
        a.start(); b.start(); a.join(30); b.join(30)
        with open(self.res) as f:
            files = [json.loads(l)["file"] for l in f]
        self.assertEqual(len(set(files)), 2)
        self.assertEqual(sorted(os.listdir(os.path.join(self.out, "NOPLP"))),
                         sorted(os.path.basename(f) for f in files))

    def test_no_french_track_falls_back_and_says_so(self):
        os.environ["FAKE_NOFRA"] = "1"
        info = self.run_rec(occ(4))
        self.assertEqual(info["state"], "done")
        self.assertEqual(info["audio"], "first (no fra tag)")
        self.assertIn("0:a:0", self.cmds()[-1])

    def test_restart_never_overwrites(self):
        o = occ(6)
        r = Recording(o, self.out, self.res)
        r.start()
        import time
        time.sleep(0.5)
        r.stop()                 # daemon killed mid-slot
        r.join(10)
        with open(self.res) as f:
            first = json.loads(f.readline())["file"]
        second = self.run_rec(o)["file"]   # restarted for the rest of the slot
        self.assertNotEqual(first, second)
        self.assertTrue(second.endswith(" - France 2 (2).ts"))
        self.assertTrue(os.path.exists(first) and os.path.exists(second))

    def test_stop(self):
        r = Recording(occ(20), self.out, self.res)
        r.start()
        import time
        time.sleep(0.5)
        r.stop()
        r.join(10)
        self.assertFalse(r.is_alive())
        with open(self.res) as f:
            self.assertEqual(json.loads(f.readlines()[-1])["state"], "stopped")

    def test_unsafe_name_refused(self):
        with self.assertRaises(ValueError):
            Recording(occ(1, name=".."), self.out, self.res)


if __name__ == "__main__":
    unittest.main()
