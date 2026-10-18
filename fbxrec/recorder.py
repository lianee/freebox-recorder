"""Recording engine: segmented ffmpeg capture with reconnect, then integrity stats."""
import json
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime

from .schedule import TZ

log = logging.getLogger("fbxrec")
FFMPEG = os.environ.get("FBXREC_FFMPEG", "ffmpeg")
FFPROBE = os.environ.get("FBXREC_FFPROBE", "ffprobe")
URL = "rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service={service}&flavour={flavour}"
RETRY_DELAY = float(os.environ.get("FBXREC_RETRY_DELAY", "5"))
# output not growing for this long = ffmpeg hung (2026-10-18: Freebox disk share stalled, the RTSP
# input died and ffmpeg waited forever, ignoring SIGTERM): kill it, the run loop retries
STALL_TIMEOUT = float(os.environ.get("FBXREC_STALL_TIMEOUT", "60"))


def safe_dirname(name):
    # names are validated upstream (no slash, no ".."); this guards the filesystem anyway
    if "/" in name or name in ("", ".", ".."):
        raise ValueError(f"unsafe name: {name!r}")
    return name


class Recording(threading.Thread):
    def __init__(self, occ, out_root, results_path, now=lambda: datetime.now(TZ)):
        super().__init__(daemon=True, name=f"rec-{occ.key}")
        self.occ, self.now = occ, now
        self.dir = os.path.join(out_root, safe_dirname(occ.name))
        stamp = occ.show_start.strftime("%Y-%m-%d %Hh%M")
        # the folder already carries the show name: the file gets date first (sorts by date) + channel.
        # A few playlist names contain "/" (e.g. "TeleBom/TeleDom"): shown as "-" in the file name.
        label = f"{stamp} - {occ.channel.replace('/', '-')}"
        self.base = os.path.join(self.dir, label)
        # restarted mid-slot (daemon or VM reboot): never overwrite the first part
        i = 2
        while os.path.exists(self.base + ".ts") or os.path.exists(self.base + ".part0.ts"):
            self.base = os.path.join(self.dir, f"{label} ({i})")
            i += 1
        self.results_path = results_path
        self.proc = None
        self.stopped = threading.Event()
        self.info = {"key": occ.key, "name": occ.name, "show_start": occ.show_start.isoformat(),
                     "start": occ.start.isoformat(), "end": occ.end.isoformat(),
                     "segments": 0, "reconnects": 0, "stalls": 0, "missed": 0, "corrupt": 0,
                     "audio": "fra", "state": "recording"}

    def stop(self):
        self.stopped.set()
        if self.proc and self.proc.poll() is None:
            self.proc.terminate()

    def _capture(self, part, seconds, audio_map):
        cmd = [FFMPEG, "-nostdin", "-v", "warning", "-rtsp_transport", "udp",
               "-i", URL.format(service=self.occ.service, flavour=self.occ.flavour),
               "-map", "0:v:0", "-map", audio_map, "-t", f"{seconds:.0f}", "-c", "copy",
               "-f", "mpegts", "-y", part]
        self.proc = subprocess.Popen(cmd, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE, text=True,
                                     errors="replace")
        size, grown, err = -1, time.monotonic(), ""
        while True:
            try:
                _, err = self.proc.communicate(timeout=min(5, STALL_TIMEOUT / 2))
                break
            except subprocess.TimeoutExpired:   # retrying communicate() loses no output
                pass
            cur = os.path.getsize(part) if os.path.exists(part) else 0
            if cur != size:
                size, grown = cur, time.monotonic()
            elif time.monotonic() - grown > STALL_TIMEOUT:
                self.info["stalls"] += 1
                log.warning("%s: no data for %.0f s, killing ffmpeg", self.occ.key, STALL_TIMEOUT)
                self.proc.kill()
        return self.proc.returncode, err

    def run(self):
        os.makedirs(self.dir, exist_ok=True)
        parts, audio_map, n, retry = [], "0:a:m:language:fra", 0, False
        while not self.stopped.is_set():
            remaining = (self.occ.end - self.now()).total_seconds()
            if remaining < 2:
                break
            if retry:   # counted only once the new capture really starts: the slot may end during the delay
                self.info["reconnects"] += 1
                retry = False
            part = f"{self.base}.part{n}.ts"
            rc, err = self._capture(part, remaining, audio_map)
            self.info["missed"] += sum(int(x) for x in re.findall(r"missed (\d+) packets", err))
            self.info["corrupt"] += len(re.findall(r"orrupt", err))
            if "matches no streams" in err and audio_map.endswith("fra"):
                # channel without a French-tagged track: fall back to the first one, and say so
                log.warning("%s: no French audio track, using the first audio track", self.occ.key)
                audio_map, self.info["audio"] = "0:a:0", "first (no fra tag)"
                continue
            if os.path.exists(part) and os.path.getsize(part) > 0:
                parts.append(part)
                n += 1
            if self.stopped.is_set():
                break
            if (self.occ.end - self.now()).total_seconds() >= 2:
                retry = True
                log.warning("%s: ffmpeg exited early (rc=%s): %s", self.occ.key, rc,
                            err.strip().splitlines()[-1:] or "")
                time.sleep(RETRY_DELAY)
        self.info["segments"] = len(parts)
        self.info["file"] = self._assemble(parts)
        self.info["state"] = "stopped" if self.stopped.is_set() else "done"
        if self.info["file"]:
            self.info["size"] = os.path.getsize(self.info["file"])
            self.info.update(pts_gaps(self.info["file"]))
        else:
            self.info["state"] = "failed"
        self.info["finished"] = datetime.now(TZ).isoformat(timespec="seconds")
        with open(self.results_path, "a") as f:
            f.write(json.dumps(self.info, ensure_ascii=False) + "\n")
        log.info("%s: %s", self.occ.key, self.info)

    def _assemble(self, parts):
        final = f"{self.base}.ts"
        if not parts:
            return None
        if len(parts) == 1:
            os.replace(parts[0], final)
            return final
        # MPEG-TS segments can be concatenated byte for byte
        with open(final, "wb") as out:
            for p in parts:
                with open(p, "rb") as src:
                    while chunk := src.read(1 << 20):
                        out.write(chunk)
        for p in parts:
            os.remove(p)
        return final


def pts_gaps(path):
    """Count video timestamp gaps between 1 and 60 s (the PVR damage signature)."""
    try:
        r = subprocess.run([FFPROBE, "-v", "quiet", "-select_streams", "v:0", "-show_entries",
                            "packet=pts_time", "-of", "csv=p=0", path],
                           capture_output=True, text=True, timeout=3600)
    except (OSError, subprocess.TimeoutExpired) as e:
        return {"gaps": None, "gap_error": str(e)}
    prev, gaps, missing = None, 0, 0.0
    for line in r.stdout.splitlines():
        line = line.strip().rstrip(",")
        if not line or line == "N/A":
            continue
        t = float(line)
        if prev is not None and 1 < t - prev < 60:
            gaps += 1
            missing += t - prev
        prev = t
    return {"gaps": gaps, "missing_s": round(missing)}
