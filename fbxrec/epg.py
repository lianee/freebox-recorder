"""TV guide from the Freebox API (LAN, no app token needed for the EPG).

GET /api/v8/tv/epg/by_time/<unix> returns about one hour of programmes for every channel
(~750 KB, 448 channels, 2026-10-20). Channel ids are "uuid-webtv-<service>", the service
number of the playlist. Numbers sometimes arrive as strings: converted explicitly."""
import json
import logging
import os
import re
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timedelta

log = logging.getLogger("fbxrec")

API = "http://mafreebox.freebox.fr/api/v8/tv/epg/by_time/{ts}"
FETCH_INTERVAL_S = 15      # between two calls to the box (its limit: ~5 a minute)
HORIZON_H = 8 * 24         # hours fetched ahead
REFRESH_S = 12 * 3600      # a cached hour is refetched after this...
REFRESH_SOON_S = 3 * 3600  # ...or after this when it starts within 6 h
MEM_HOURS = 24             # parsed hours kept in memory
FIND_WINDOW_S = 2 * 3600   # a followed show may start this far from its fixed time
MERGE_GAP_S = 600          # consecutive parts of one show separated by at most this are merged
FOUND_CACHE_S = 600
DESC_MAX = 400
PICTURE_RE = re.compile(r"^/api/[\w./-]{1,200}\.(jpg|jpeg|png)$")


class EpgError(Exception):
    pass


def _int(v, what):
    # the API mixes 300 and "300": accept both, reject anything else by name
    if isinstance(v, bool):
        raise EpgError(f"{what}: {v!r}")
    if isinstance(v, int) and v >= 0:
        return v
    if isinstance(v, str) and v.isdigit():
        return int(v)
    raise EpgError(f"{what}: {v!r}")


def parse(data):
    """{service: {start: programme}} from one by_time answer."""
    if not isinstance(data, dict) or data.get("success") is not True or not isinstance(data.get("result"), dict):
        raise EpgError("unexpected answer")
    out = {}
    for uuid, progs in data["result"].items():
        if not uuid.startswith("uuid-webtv-") or not uuid[11:].isdigit() or not isinstance(progs, dict):
            continue
        svc = int(uuid[11:])
        for p in progs.values():
            start = _int(p.get("date"), "date")
            out.setdefault(svc, {})[start] = {
                "start": start, "duration": _int(p.get("duration"), "duration"),
                "title": str(p.get("title") or "")[:200], "category": str(p.get("category_name") or "")[:60],
                "desc": str(p.get("desc") or "")[:DESC_MAX], "sub_title": str(p.get("sub_title") or "")[:200]}
            # picture served by the box itself (no auth, not rate-limited, ~4 KB): the page loads
            # it from mafreebox.freebox.fr; only a plain image path of its API is kept
            pic = p.get("picture_big") or p.get("picture")
            if isinstance(pic, str) and PICTURE_RE.match(pic) and ".." not in pic:
                out[svc][start]["picture"] = pic
            for k in ("season_number", "episode_number"):   # series only
                if p.get(k) not in (None, "", 0, "0"):
                    out[svc][start][k[:-7]] = _int(p[k], k)
    return out


def norm(title):
    """Comparable form of a title: case, accents kept, punctuation and spacing ignored."""
    return " ".join(re.sub(r"[^\w]+", " ", title.casefold()).split())


class Guide:
    """Guide kept on disk, filled by one paced background fetcher; pages only read it.

    The box rate-limits this API (measured 2026-10-20: bursts of 3, about 5 calls a minute,
    refused calls seem to count, no Retry-After): fetching on demand failed with HTTP 429 after
    three day changes in the page. One call every FETCH_INTERVAL_S stays well below."""

    def __init__(self, cache_dir, fetch=None, now=time.time):
        self.dir, self.fetch, self.now = cache_dir, fetch or self._fetch, now
        os.makedirs(cache_dir, exist_ok=True)
        self.mem, self.urgent, self.lock = {}, [], threading.Lock()
        self.backoff = 0
        self.found = {}   # find() results, short-lived

    @staticmethod
    def _fetch(ts):
        with urllib.request.urlopen(API.format(ts=ts), timeout=15) as r:
            return json.loads(r.read())

    def _path(self, ts):
        return os.path.join(self.dir, f"{ts}.json")

    def _age(self, ts):
        try:
            return self.now() - os.path.getmtime(self._path(ts))
        except FileNotFoundError:
            return None

    def hour(self, ts):
        """Cached programmes of one hour, or None if not fetched yet."""
        ts -= ts % 3600
        mtime = self._age(ts)
        if mtime is None:
            return None
        with self.lock:
            hit = self.mem.get(ts)
            if hit and hit[0] == mtime:
                return hit[1]
        with open(self._path(ts)) as f:
            raw = json.load(f)
        progs = {int(svc): {int(k): v for k, v in p.items()} for svc, p in raw.items()}
        with self.lock:
            self.mem[ts] = (mtime, progs)
            for k in sorted(self.mem)[:-MEM_HOURS]:
                del self.mem[k]
        return progs

    def window(self, start, hours, services):
        """(programmes overlapping [start, start + hours h) per service, hours not fetched yet).
        Missing hours jump to the front of the fetch queue."""
        start -= start % 3600
        end = start + hours * 3600
        merged, missing = {}, []
        for h in range(hours):
            ts = start + h * 3600
            progs = self.hour(ts)
            if progs is None:
                missing.append(ts)
                continue
            for svc, p in progs.items():
                if svc in services:
                    merged.setdefault(svc, {}).update(p)
        if missing:
            with self.lock:
                self.urgent = missing + [t for t in self.urgent if t not in missing]
        out = {str(svc): sorted((p for p in progs.values() if p["start"] < end and p["start"] + p["duration"] > start),
                                key=lambda p: p["start"])
               for svc, progs in merged.items()}
        return out, missing

    def find(self, service, title, around, window=FIND_WINDOW_S, gap=MERGE_GAP_S):
        """(start, end) of the programme `title` on `service`, consecutive parts of the same title
        (gap <= `gap`) merged. Looked for within `window` of `around` first; else later that
        evening and night until 06:00 next morning (a show moved for a match, say), else earlier
        that day from 06:00 (later first: a morning slot is more likely a rerun). None when not
        found or when the guide is not loaded there (the caller keeps its fixed times)."""
        from .schedule import TZ   # local: schedule imports nothing from here, keep it so
        want = norm(title)
        key = (service, want, around)
        with self.lock:
            hit = self.found.get(key)
        if hit and self.now() - hit[0] < FOUND_CACHE_S:
            return hit[1]
        progs, loaded = {}, set()

        def load(ts):
            ts -= ts % 3600
            if ts in loaded:
                return True
            p = self.hour(ts)
            if p is None:
                return False
            loaded.add(ts)
            progs.update(p.get(service, {}))
            return True

        def first_in(lo, hi, latest_first=False):
            """First showing starting in [lo, hi], or None; False if the guide lacks an hour there."""
            if not all(load(t) for t in range(lo, hi + 1, 3600)) or not load(hi):
                return False
            hits = [p for p in progs.values() if norm(p["title"]) == want and lo <= p["start"] <= hi]
            if not hits:
                return None
            return (max if latest_first else min)(hits, key=lambda p: p["start"])

        local = datetime.fromtimestamp(around, TZ)
        morning = local.replace(hour=6, minute=0, second=0, microsecond=0)
        if local < morning:          # a show fixed after midnight belongs to the evening before
            morning -= timedelta(days=1)
        day_start = int(morning.timestamp())
        night_end = int((morning + timedelta(days=1)).timestamp())
        first = first_in(around - window, around + window)
        if first is None:
            first = first_in(around + window + 1, night_end)
        if first is None:
            # earlier that day: the closest to the usual time
            first = first_in(day_start, around - window - 1, latest_first=True)
        result = None
        if first:
            start, end = first["start"], first["start"] + first["duration"]
            while True:   # following parts, loading later hours as needed
                load(end + gap)
                nxt = [p for p in sorted(progs.values(), key=lambda p: p["start"])
                       if norm(p["title"]) == want and end - 60 <= p["start"] <= end + gap
                       and p["start"] + p["duration"] > end]
                if not nxt:
                    break
                end = nxt[0]["start"] + nxt[0]["duration"]
            result = (start, end)
        with self.lock:
            self.found[key] = (self.now(), result)
        return result

    def recurrence(self, service, title, start, window=FIND_WINDOW_S):
        """Days (0 = Monday, Europe/Paris) on which `title` is shown on `service` within `window`
        of the same time of day, over the cached days, and how many showings. Reads only the
        hours around that time each day (a full scan of the cache takes 2.6 s on the VM)."""
        from .schedule import TZ   # local: schedule imports nothing from here, keep it so
        want, days, count, seen = norm(title), set(), 0, set()
        for k in range(-2, HORIZON_H // 24 + 2):
            centre = start + k * 86400
            for ts in range(centre - window, centre + window + 1, 3600):
                progs = self.hour(ts)
                for st, p in (progs or {}).get(service, {}).items():
                    if st in seen or abs(st - centre) > window or norm(p["title"]) != want:
                        continue
                    seen.add(st)
                    count += 1
                    days.add(datetime.fromtimestamp(st, TZ).weekday())
        return {"days": sorted(days), "count": count}

    def stale(self, ts):
        age = self._age(ts)
        if age is None:
            return True
        soon = ts - self.now() < 6 * 3600
        return age > (REFRESH_SOON_S if soon else REFRESH_S)

    def next_slot(self):
        """Hour to fetch next: asked for by a page first, then the coming days, soonest first."""
        with self.lock:
            while self.urgent:
                ts = self.urgent.pop(0)
                if self._age(ts) is None:
                    return ts
        now = int(self.now())
        first = now - now % 3600
        for ts in range(first, first + HORIZON_H * 3600, 3600):
            if self.stale(ts):
                return ts
        return None

    def fetch_one(self):
        """Fetch one hour if one is due. Returns seconds to wait before the next call."""
        ts = self.next_slot()
        if ts is None:
            return 60
        try:
            progs = parse(self.fetch(ts))
        except urllib.error.HTTPError as e:
            if e.code == 429:   # over the box's limit: back off, doubling up to 10 min
                self.backoff = min(600, self.backoff * 2 or 60)
                log.warning("guide: Freebox says 429, waiting %d s", self.backoff)
                with self.lock:
                    self.urgent.insert(0, ts)
                return self.backoff
            log.warning("guide: %s", e)
            return 120
        except (OSError, ValueError, EpgError) as e:
            log.warning("guide: %s", e)
            return 120
        self.backoff = 0
        tmp = self._path(ts) + ".tmp"
        with open(tmp, "w") as f:
            json.dump(progs, f, ensure_ascii=False)
        now = self.now()
        os.utime(tmp, (now, now))   # the age of an hour follows the guide's clock
        os.replace(tmp, self._path(ts))
        self._prune()
        return FETCH_INTERVAL_S

    def _prune(self):
        limit = self.now() - 86400
        for f in os.listdir(self.dir):
            if f.endswith(".json") and f[:-5].isdigit() and int(f[:-5]) < limit:
                os.remove(os.path.join(self.dir, f))
