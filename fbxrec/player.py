"""Check player: a recorded MPEG-TS remuxed on the fly to fragmented MP4 (no re-encoding).

Browsers do not play MPEG-TS. ffmpeg -c copy only rewraps the packets: negligible CPU on the
VM when paced (-readrate), nothing written to disk. Seeking = a new request starting at another offset."""
import os
import re
import subprocess
import threading

from .recorder import FFMPEG, FFPROBE

MAX_STREAMS = 2   # remuxes at once (each reads the Freebox disk share)
slots = threading.BoundedSemaphore(MAX_STREAMS)


class NotFound(Exception):
    pass


def resolve(out_root, rel, live_parts=()):
    """Absolute path of a recording given relative to the output folder, or NotFound.
    Rejects anything that is not an existing .ts or .mkv file inside out_root; a capture part
    (.partN.ts) only when it is in live_parts (recordings in progress)."""
    if not isinstance(rel, str) or not rel or "\x00" in rel or rel.startswith("/") \
            or not rel.endswith((".ts", ".mkv")):
        raise NotFound(rel)
    is_part = re.search(r"\.part\d+\.ts$", rel)
    root = os.path.realpath(out_root)
    path = os.path.realpath(os.path.join(root, rel))
    if not path.startswith(root + os.sep) or not os.path.isfile(path):
        raise NotFound(rel)
    if is_part and path not in {os.path.realpath(p) for p in live_parts}:
        raise NotFound(rel)
    return path


def duration(path):
    """Seconds, from ffprobe (reads the start and the end of the file), or None."""
    try:
        r = subprocess.run([FFPROBE, "-v", "error", "-show_entries", "format=duration", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=60)
        return round(float(r.stdout.strip()), 1)
    except (OSError, subprocess.TimeoutExpired, ValueError):
        return None


def remux_cmd(path, t, clip):
    # delay_moov: the header waits for the first packets, else aac_adtstoasc has not produced the
    # AudioSpecificConfig yet and the MP4 says plain AAC 48 kHz (HE-AAC: decode errors in every browser)
    # read at full speed a remux takes 30-50 % of the 1 vCPU (browsers buffer ahead in bursts);
    # -readrate 2 (twice real time, enough to play and buffer) measured ~1 % on the VM.
    # nice 19 on top: recordings always come first.
    return ["nice", "-n", "19", FFMPEG, "-nostdin", "-v", "error", "-readrate", "2", "-ss", f"{t:.3f}", "-i", path, "-t", f"{clip:.0f}",
            "-map", "0:v:0", "-map", "0:a:0?", "-c", "copy",
            "-bsf:a", "aac_adtstoasc",   # ADTS AAC (TS) must be rewrapped for MP4, ffmpeg refuses otherwise
            "-f", "mp4", "-movflags", "frag_keyframe+empty_moov+default_base_moof+delay_moov", "pipe:1"]


def stream(path, t, out, clip):
    """Write the MP4 to the file-like `out` until the end or until the client goes away."""
    proc = subprocess.Popen(remux_cmd(path, t, clip), stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
    try:
        while chunk := proc.stdout.read(64 * 1024):
            out.write(chunk)
    except OSError:   # browser closed or seeked elsewhere (broken pipe, socket timeout)
        pass
    finally:
        proc.kill()
        proc.wait()
