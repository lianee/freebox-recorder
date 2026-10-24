"""Background conversion of recorded MPEG-TS files to MKV, the format players open.

Why (measured 2026-10-20): Windows' player opens .ts files only now and then; Ubuntu's
"Lecteur vidéo" (Showtime) crashes on any interlaced H.264, whatever the container, unless
the container says "progressive", which only MKV can say. So: video copied untouched and
flagged progressive (combing visible on fast motion, no automatic deinterlacing), audio
re-encoded to AAC-LC (ffmpeg 5.1 aborts the copy on the damaged ADTS frames of a broadcast),
video packets without timestamps dropped (one in 3414 in a NOPLP sample: the MKV muxer
refuses them). About 12x real time at nice 19 on the 1 vCPU VM.

Never runs during a recording (CPU and Freebox disk share); a recording starting stops it."""
import logging
import os
import re
import subprocess
import time

from .recorder import FFMPEG
from .player import duration

log = logging.getLogger("fbxrec")
TOLERANCE_S = 2   # MKV and TS durations must match this closely before the TS is deleted


class ConvertError(Exception):
    pass


class Interrupted(ConvertError):
    """Stopped on purpose (a recording comes): not a failure, retried later."""


def cmd(src, dst):
    return ["nice", "-n", "19", FFMPEG, "-nostdin", "-v", "error", "-i", src,
            "-map", "0:v:0", "-map", "0:a:0", "-c:v", "copy", "-bsf:v", "noise=drop=eq(pts\\,nopts)",
            "-c:a", "aac", "-b:a", "128k", "-field_order", "progressive", "-f", "matroska", "-y", dst]


def candidates(out_root, busy_bases=(), min_age_s=120):
    """Finished .ts files, newest first. Skips hidden and "_" folders (kept originals),
    capture parts, files still growing, and recordings in progress."""
    found = []
    for top, dirs, files in os.walk(out_root):
        dirs[:] = [x for x in dirs if not x.startswith((".", "_"))]
        for f in files:
            if not f.endswith(".ts") or re.search(r"\.part\d+\.ts$", f):
                continue
            p = os.path.join(top, f)
            try:
                st = os.stat(p)
            except OSError:
                continue
            if time.time() - st.st_mtime < min_age_s or p[:-3] in busy_bases:
                continue
            found.append((st.st_mtime, p))
    return [p for _, p in sorted(found, reverse=True)]


def convert(ts, should_stop=lambda: False, poll_s=5):
    """TS -> MKV next to it, TS deleted once the MKV is verified. Returns the MKV path.
    should_stop() is polled while ffmpeg runs: True kills it and raises ConvertError."""
    mkv, tmp = ts[:-3] + ".mkv", ts[:-3] + ".mkv.tmp"
    if os.path.exists(mkv):
        # an MKV only gets its final name after verification: a TS still beside it means
        # the process stopped between the rename and the delete
        os.remove(ts)
        return mkv
    want = duration(ts)
    if not want:
        raise ConvertError(f"{ts}: duration unknown")
    proc = subprocess.Popen(cmd(ts, tmp), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                            errors="replace")
    while True:
        try:
            _, err = proc.communicate(timeout=poll_s)
            break
        except subprocess.TimeoutExpired:
            if should_stop():
                proc.kill()
                proc.communicate()
                _remove(tmp)
                raise Interrupted(f"{ts}: interrupted") from None
    got = duration(tmp) if proc.returncode == 0 else None
    if got is None or abs(got - want) > TOLERANCE_S:
        _remove(tmp)
        last = [l for l in err.splitlines() if "Number of bands" not in l][-1:]
        raise ConvertError(f"{ts}: rc={proc.returncode}, duration {got} vs {want}, {last}")
    os.replace(tmp, mkv)
    os.remove(ts)
    log.info("converted %s (%.0f s)", mkv, got)
    return mkv


def _remove(p):
    try:
        os.remove(p)
    except FileNotFoundError:
        pass
