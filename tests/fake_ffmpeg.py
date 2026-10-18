#!/usr/bin/env python3
"""Fake ffmpeg/ffprobe for tests. Behaviour driven by env vars:
FAKE_RUN: seconds each capture lasts before exiting early (default: full -t)
FAKE_NOFRA=1: first call fails like ffmpeg when the fra audio map matches nothing
FAKE_LOG: file where each command line is appended.
FAKE_STALL_ONCE=<marker>: first capture writes a little, then hangs ignoring SIGTERM
  (like ffmpeg when the Freebox disk share stalls and the RTSP input dies, 2026-10-18)."""
import os, signal, sys, time, json
args = sys.argv[1:]
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(args) + "\n")
if os.path.basename(sys.argv[0]).startswith("fake_ffprobe"):
    for t in (0, 0.04, 0.08, 5.08, 5.12):   # one 5 s gap
        print(t)
    sys.exit(0)
if os.environ.get("FAKE_NOFRA") and "0:a:m:language:fra" in args:
    sys.stderr.write("Stream map '0:a:m:language:fra' matches no streams.\n")
    sys.exit(1)
dur = float(args[args.index("-t") + 1])
run = min(dur, float(os.environ.get("FAKE_RUN", dur)))
with open(args[-1], "wb") as f:
    f.write(b"G" * 188)
marker = os.environ.get("FAKE_STALL_ONCE")
if marker and not os.path.exists(marker):
    open(marker, "w").close()
    signal.signal(signal.SIGTERM, signal.SIG_IGN)
    time.sleep(60)
if os.environ.get("FAKE_GROW"):
    end = time.monotonic() + run
    with open(args[-1], "ab") as f:
        while time.monotonic() < end:
            f.write(b"G" * 188)
            f.flush()
            time.sleep(0.2)
else:
    time.sleep(run)
sys.stderr.write("RTP: missed 2 packets\n")
sys.exit(0 if run >= dur else 1)
