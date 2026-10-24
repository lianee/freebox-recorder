"""Automatic deletion of old recordings, per folder ("keep N days", unset = never).

A recording's age comes from the show date at the start of its name ("2026-10-20 18h40 - ..."),
not from the file time: the MKV conversion rewrote every file, which reset their times.
Only video files directly in the folder are deleted: other content (tv-extract results in
subfolders, notes) stays. Folders starting with "_" or "." are never listed nor touched."""
import json
import logging
import os
import re
import threading
from datetime import datetime, timedelta

from .schedule import TZ

log = logging.getLogger("fbxrec")
VIDEO = (".mkv", ".ts")
DATE_RE = re.compile(r"^(\d{4}-\d{2}-\d{2}) (\d{2})h(\d{2})")
MAX_DAYS = 3650


class RetentionError(ValueError):
    pass


def show_time(name, mtime):
    m = DATE_RE.match(name)
    if m:
        try:
            return datetime.strptime(f"{m.group(1)} {m.group(2)}:{m.group(3)}", "%Y-%m-%d %H:%M").replace(tzinfo=TZ)
        except ValueError:
            pass
    return datetime.fromtimestamp(mtime, TZ)


def folders(out_root):
    try:
        names = sorted(n for n in os.listdir(out_root)
                       if not n.startswith(("_", ".")) and os.path.isdir(os.path.join(out_root, n)))
    except OSError:
        return []
    return names


def videos(folder):
    out = []
    for n in os.listdir(folder):
        p = os.path.join(folder, n)
        if n.endswith(VIDEO) and not re.search(r"\.part\d+\.ts$", n) and os.path.isfile(p):
            st = os.stat(p)
            out.append((p, st.st_size, show_time(n, st.st_mtime)))
    return out


class Retention:
    def __init__(self, path, out_root):
        self.path, self.out_root, self.lock = path, out_root, threading.Lock()

    def rules(self):
        try:
            with open(self.path) as f:
                r = json.load(f)
        except FileNotFoundError:
            return {}
        if not isinstance(r, dict) or not all(isinstance(v, int) and not isinstance(v, bool) for v in r.values()):
            raise RetentionError("retention file unreadable")
        return r

    def set(self, folder, days):
        if not isinstance(folder, str) or folder not in folders(self.out_root):
            raise RetentionError("unknown folder")
        if days is not None and (isinstance(days, bool) or not isinstance(days, int) or not 1 <= days <= MAX_DAYS):
            raise RetentionError(f"days: 1 to {MAX_DAYS}, or null for never")
        with self.lock:
            r = self.rules()
            if days is None:
                r.pop(folder, None)
            else:
                r[folder] = days
            tmp = self.path + ".tmp"
            with open(tmp, "w") as f:
                json.dump(r, f, ensure_ascii=False)
            os.replace(tmp, self.path)

    def summary(self):
        r = self.rules()
        out = []
        for name in folders(self.out_root):
            v = videos(os.path.join(self.out_root, name))
            out.append({"folder": name, "files": len(v), "size": sum(s for _, s, _ in v), "days": r.get(name)})
        return out

    def apply(self, now, busy_bases=()):
        """Delete videos older than their folder's rule. Returns the deleted paths."""
        deleted = []
        for name, days in self.rules().items():
            folder = os.path.join(self.out_root, name)
            if name not in folders(self.out_root):
                continue
            limit = now - timedelta(days=days)
            for p, _, shown in videos(folder):
                if shown < limit and p.rsplit(".", 1)[0] not in busy_bases:
                    os.remove(p)
                    deleted.append(p)
                    log.info("retention (%d days): deleted %s", days, p)
        return deleted
