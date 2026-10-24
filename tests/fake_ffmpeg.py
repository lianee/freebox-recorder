#!/usr/bin/env python3
"""Fake ffmpeg/ffprobe for tests. Behaviour driven by env vars:
FAKE_RUN: seconds each capture lasts before exiting early (default: -t if given, else until SIGTERM,
  like the real ffmpeg reading a live stream)
FAKE_NOFRA=1: first call fails like ffmpeg when the fra audio map matches nothing
FAKE_LOG: file where each command line is appended.
FAKE_CONVERT=fail|slow: MKV conversion exits 1, or runs 30 s (default: writes the output, exits 0)
FAKE_MKV_DURATION: duration ffprobe reports for a .mkv.tmp (default: same as any file)
FAKE_STALL_ONCE=<marker>: first capture writes a little, then hangs ignoring SIGTERM
  (like ffmpeg when the Freebox disk share stalls and the RTSP input dies, 2026-10-18)."""
import os, signal, sys, time, json
args = sys.argv[1:]
if os.environ.get("FAKE_LOG"):
    with open(os.environ["FAKE_LOG"], "a") as f:
        f.write(json.dumps(args) + "\n")
if os.path.basename(sys.argv[0]).startswith("fake_ffprobe"):
    if "format=duration" in args:
        print(os.environ.get("FAKE_MKV_DURATION", "5400.5") if args[-1].endswith(".mkv.tmp") else "5400.5")
        sys.exit(0)
    for t in (0, 0.04, 0.08, 5.08, 5.12):   # one 5 s gap
        print(t)
    sys.exit(0)
if os.environ.get("FAKE_NOFRA") and "0:a:m:language:fra" in args:
    sys.stderr.write("Stream map '0:a:m:language:fra' matches no streams.\n")
    sys.exit(1)
if "matroska" in args:   # convert.py
    mode = os.environ.get("FAKE_CONVERT", "")
    with open(args[-1], "wb") as f:
        f.write(b"MKV")
    if mode == "slow":
        time.sleep(30)
    sys.exit(1 if mode == "fail" else 0)
if args[-1] == "pipe:1":   # player remux: a short fake MP4 on stdout
    sys.stdout.buffer.write(b"MP4" * 1000)
    sys.exit(0)
dur = float(args[args.index("-t") + 1]) if "-t" in args else float("inf")


def on_term(*_):   # real ffmpeg: "Exiting normally, received signal 15", rc 255
    sys.stderr.write("RTP: missed 2 packets\n")
    sys.exit(255)


signal.signal(signal.SIGTERM, on_term)
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
    end = time.monotonic() + run   # time.sleep(inf) raises OverflowError
    while time.monotonic() < end:
        time.sleep(0.2)
sys.stderr.write("RTP: missed 2 packets\n")
sys.exit(0 if run >= dur else 1)
