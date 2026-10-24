"""fbxrec daemon: scheduler loop + LAN web UI/API (LAN only, optional password, see auth.py)."""
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.parse
import urllib.request
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import REPO, __version__, auth, convert, epg, notify, player, retention, update
from . import schedule as sch
from .auth import Guard
from .recorder import Recording

log = logging.getLogger("fbxrec")
PLAYLIST = "http://mafreebox.freebox.fr/freeboxtv/playlist.m3u"
STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 16 * 1024
PLAY_CLIP_S = 600        # one player request streams at most this much (seeking = a new request)
PLAY_IDLE_S = 120        # a paused video lets the browser stop reading: give up after this
REBOOT_MARGIN_MIN = 10   # no reboot or update when a recording starts this soon (either takes ~1 min)
CONVERT_IDLE_MIN = 20    # start a conversion only if no recording starts within this
CONVERT_TICK_S = 60
DISK_ALERT_GB = 10       # SMS when less is free on the Freebox disk (once a day at most)
WATCH_TICK_S = 600
UPCOMING_DAYS = 7         # the page lists every recording of the coming week
INSTANT_NAME = "Immédiat"   # folder of instant recordings (named after the button)
INSTANT_MAX = 4          # instant recordings at once
INSTANT_MAX_MIN = 24 * 60


def parse_playlist(text):
    """Channels from the Freebox m3u: [{number, name, service, flavours}]."""
    chans, label = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith("#EXTINF"):
            label = line.split(",", 1)[1] if "," in line else None
        elif line.startswith("rtsp://") and label:
            m = re.match(r"^(\d+) - (.+?)(?: \((auto|HD|standard|bas débit|[^)]*)\))?$", label)
            svc = re.search(r"[?&]service=(\d+)", line)
            fl = re.search(r"[?&]flavour=(\w+)", line)
            if m and svc:
                num, name, s = int(m.group(1)), m.group(2), int(svc.group(1))
                c = chans.setdefault(s, {"number": num, "name": name, "service": s, "flavours": []})
                if fl and fl.group(1) not in c["flavours"]:
                    c["flavours"].append(fl.group(1))
            label = None
    return sorted(chans.values(), key=lambda c: (c["number"], c["name"]))


class Store:
    def __init__(self, path):
        self.path, self.lock = path, threading.Lock()
        if not os.path.exists(path):
            self._write([])

    def all(self):
        with self.lock, open(self.path) as f:
            return json.load(f)

    def _write(self, items):
        tmp = self.path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(items, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.path)

    def put(self, s):
        with self.lock:
            with open(self.path) as f:
                items = json.load(f)
            items = [x for x in items if x["id"] != s["id"]] + [s]
            self._write(items)

    def delete(self, sid):
        with self.lock:
            with open(self.path) as f:
                items = json.load(f)
            kept = [x for x in items if x["id"] != sid]
            self._write(kept)
            return len(kept) != len(items)


def forever(fn, every_s, what, first_wait=False):
    """Run fn() for ever, every every_s seconds (or the seconds fn returns), logging failures."""
    if first_wait:
        time.sleep(every_s)
    while True:
        try:
            wait = fn()
        except Exception:
            log.exception("%s failed", what)
            wait = None
        time.sleep(wait if isinstance(wait, (int, float)) and not isinstance(wait, bool) else every_s)


def alert_text(info):
    """SMS text for a finished recording that went wrong, else None."""
    what = f"« {info.get('name')} » du {datetime.fromisoformat(info['show_start']).strftime('%d/%m %Hh%M')}"
    if info.get("channel"):
        what += f" ({info['channel']})"
    if info.get("state") == "failed":
        return f"Enregistreur TV : {what} : échec, rien n'a été enregistré."
    bad = []
    if info.get("gaps"):
        bad.append(f"{info['gaps']} trou(s) dans l'image ({info.get('missing_s', 0)} s)")
    if info.get("reconnects"):
        bad.append(f"{info['reconnects']} coupure(s) du flux")
    if info.get("stalls"):
        bad.append(f"{info['stalls']} blocage(s) du disque")
    return f"Enregistreur TV : {what} : " + ", ".join(bad) + "." if bad else None


class Daemon:
    def __init__(self, data_dir, out_root, tick=10, now=lambda: datetime.now(sch.TZ)):
        self.store = Store(os.path.join(data_dir, "schedules.json"))
        # instant recordings: kept on disk so a restart (service or VM) resumes them
        self.instant = Store(os.path.join(data_dir, "instant.json"))
        # end moved on a running scheduled recording, by occurrence key: survives a restart
        self.ends = Store(os.path.join(data_dir, "end_overrides.json"))
        self.results = os.path.join(data_dir, "results.jsonl")
        self.out_root, self.tick, self.now = out_root, tick, now
        self.running = {}   # occurrence key -> Recording
        self.started = set()
        self.channels, self.lock = [], threading.Lock()
        self.guide = epg.Guide(os.path.join(data_dir, "epg"))
        self.convert_failed = set()
        self.converting = None   # .ts being converted (the page must not delete it meanwhile)
        self.sms = notify.Sms(os.path.join(data_dir, "sms.json"))
        self.disk_alerted = 0.0
        self.moved_alerted = set()   # occurrence keys whose move was already sent by SMS
        self.retention = retention.Retention(os.path.join(data_dir, "retention.json"), out_root)
        self.favorites_path = os.path.join(data_dir, "favorites.json")

    def favorites(self):
        try:
            with open(self.favorites_path) as f:
                fav = json.load(f)
        except FileNotFoundError:
            return []
        return fav if isinstance(fav, list) and all(isinstance(x, int) and not isinstance(x, bool) for x in fav) else []

    def set_favorites(self, services):
        """Favourite channels (service numbers), shared by every device: kept on the VM."""
        if not isinstance(services, list) or len(services) > 1000 or not all(
                isinstance(x, int) and not isinstance(x, bool) and 1 <= x <= 99999 for x in services):
            raise sch.ValidationError("services: list of channel service numbers")
        fav = sorted(set(services))
        tmp = self.favorites_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(fav, f)
        os.replace(tmp, self.favorites_path)
        return fav

    def refresh_channels(self):
        try:
            with urllib.request.urlopen(PLAYLIST, timeout=15) as r:
                self.channels = parse_playlist(r.read().decode("utf-8", "replace"))
        except OSError as e:
            log.warning("playlist unavailable: %s", e)

    def instant_occurrences(self):
        out = []
        for i in self.instant.all():
            st, en = datetime.fromisoformat(i["start"]), datetime.fromisoformat(i["end"])
            out.append(sch.Occurrence(i["id"], INSTANT_NAME, i["service"], i["flavour"], st, en,
                                      st.astimezone(sch.TZ), i["channel"]))
        return out

    def adjust(self, s, o):
        """Occurrence o of schedule s with the guide's times when s follows the guide and the
        programme is found; else o unchanged. The key and file name keep the fixed show start,
        so a guide update never starts the same show twice."""
        if not s.get("follow_guide"):
            return o
        hit = self.guide.find(o.service, s["guide_title"], int(o.show_start.timestamp()))
        if not hit:
            return o
        st, en = (datetime.fromtimestamp(t, sch.UTC) for t in hit)
        return replace(o, start=st - timedelta(minutes=s["margin_before"]),
                       end=en + timedelta(minutes=s["margin_after"]), guided=True)

    def occurrences(self, scheds, now, limit=20, horizon_days=31):
        """Upcoming or running occurrences, guide applied, soonest first."""
        by_id = {s["id"]: s for s in scheds}
        # from a day back: a followed show may be moved late into the night (epg.Guide.find);
        # one more day of horizon, which counts from that day back
        out = [self.adjust(by_id[o.schedule_id], o) for o in
               sch.upcoming(scheds, now - timedelta(hours=24), limit=1000, horizon_days=horizon_days + 1)]
        out = [o for o in out if o.end > now]
        out.sort(key=lambda o: o.start)
        return out[:limit]

    def step(self):
        now = self.now()
        with self.lock:
            for k in [k for k, r in self.running.items() if not r.is_alive()]:
                del self.running[k]
            instant = []
            for o in self.instant_occurrences():
                if o.end <= now:
                    self.instant.delete(o.schedule_id)
                else:
                    instant.append(o)
            ends = {}
            for e in self.ends.all():
                end = datetime.fromisoformat(e["end"])
                if end <= now:
                    self.ends.delete(e["id"])
                else:
                    ends[e["id"]] = end
            scheds = self.store.all()
            due = [o for o in self.occurrences(scheds, now, limit=1000, horizon_days=2) if o.start <= now]
            # a running followed show whose guide end changed (and not moved by hand)
            for o in due:
                r = self.running.get(o.key)
                if r and r.is_alive() and o.key not in ends and o.guided and r.end != o.end:
                    log.info("%s: end follows the guide, now %s", o.key, o.end.isoformat())
                    r.set_end(o.end)
            # restarted after the slot but before a moved end: rebuild that occurrence
            keys = {o.key for o in due}
            for key in ends:
                sid, _, show = key.partition("@")
                s = next((x for x in scheds if x["id"] == sid and x["enabled"]), None)
                if s and key not in keys:
                    try:
                        o = sch.occurrence_on(s, datetime.fromisoformat(show).date())
                    except ValueError:
                        continue
                    if o and o.key == key and o.start <= now:
                        due.append(o)
            for o, is_instant in [(o, False) for o in due] + \
                    [(o, True) for o in instant if o.start <= now]:
                if o.key not in self.started:
                    self.started.add(o.key)
                    rec = Recording(o, self.out_root, self.results, self.now, self.on_result)
                    if o.key in ends:
                        rec.set_end(ends[o.key])
                    rec.info["instant"] = is_instant
                    self.running[o.key] = rec
                    log.info("start %s until %s", o.key, o.end.isoformat())
                    rec.start()

    def start_instant(self, body):
        """Record a channel now, into INSTANT_NAME. Returns the running key."""
        extra = set(body) - {"service", "flavour", "minutes"}
        if extra:
            raise sch.ValidationError(f"unknown fields: {', '.join(sorted(extra))}")
        svc, fl, mins = body.get("service"), body.get("flavour", "sd"), body.get("minutes", 60)
        if isinstance(svc, bool) or not isinstance(svc, int):
            raise sch.ValidationError("field service must be int")
        ch = next((c for c in self.channels if c["service"] == svc), None)
        if not ch:
            raise sch.ValidationError("unknown channel")
        if fl not in sch.FLAVOURS:
            raise sch.ValidationError("flavour must be sd or hd")
        if isinstance(mins, bool) or not isinstance(mins, int) or not 1 <= mins <= INSTANT_MAX_MIN:
            raise sch.ValidationError(f"minutes: integer 1 to {INSTANT_MAX_MIN}")
        if len(self.instant.all()) >= INSTANT_MAX:
            raise sch.ValidationError(f"at most {INSTANT_MAX} instant recordings at once")
        start = self.now().astimezone(sch.UTC).replace(microsecond=0)
        item = {"id": uuid.uuid4().hex[:12], "service": svc, "channel": ch["name"], "flavour": fl,
                "start": start.isoformat(), "end": (start + timedelta(minutes=mins)).isoformat()}
        self.instant.put(item)
        self.step()
        return f"{item['id']}@{start.astimezone(sch.TZ).isoformat()}"

    def set_running_end(self, key, hhmm):
        """Move the end of a running recording to the next HH:MM (Europe/Paris). Kept on disk
        (instant.json, or end_overrides.json for a scheduled one) so a restart keeps it."""
        if not isinstance(hhmm, str) or not sch.HHMM_RE.match(hhmm):
            raise sch.ValidationError("end must be HH:MM")
        r = self.running.get(key) if isinstance(key, str) else None
        if not r or not r.is_alive():
            return None
        now = self.now().astimezone(sch.TZ)
        h, m = map(int, hhmm.split(":"))
        end = now.replace(hour=h, minute=m, second=0, microsecond=0)
        if end <= now:
            end = sch._local(now.date() + timedelta(days=1), hhmm)
        end = end.astimezone(sch.UTC)
        if r.info.get("instant"):
            item = next((i for i in self.instant.all() if i["id"] == r.occ.schedule_id), None)
            if item:
                self.instant.put(dict(item, end=end.isoformat()))
        else:
            self.ends.put({"id": r.occ.key, "end": end.isoformat()})
        r.set_end(end)
        return end

    def busy(self, within_min):
        """Why heavy work (reboot, conversion) must wait: a recording runs or starts soon, else None."""
        if any(r.is_alive() for r in list(self.running.values())):
            return "enregistrement en cours, réessayez après"
        now = self.now()
        for o in self.occurrences(self.store.all(), now, limit=1, horizon_days=2):
            if o.start - now < timedelta(minutes=within_min):
                return f"un enregistrement commence dans moins de {within_min} minutes ({o.name})"
        return None

    def on_result(self, info):
        msg = alert_text(info)
        if msg:
            self.sms.alert(msg)

    def check_disk(self):
        try:
            st = os.statvfs(self.out_root)
        except OSError as e:
            log.warning("disk check: %s", e)
            return
        free = st.f_bavail * st.f_frsize
        if free < DISK_ALERT_GB * 1e9 and time.time() - self.disk_alerted > 86400:
            self.disk_alerted = time.time()
            self.sms.alert(f"Enregistreur TV : plus que {free / 1e9:.1f} Go libres sur le disque de la Freebox.")

    def watch_step(self):
        self.retention.apply(self.now(), {r.base for r in list(self.running.values())})
        self.check_disk()
        self.check_moves()

    def check_moves(self):
        """SMS once when a followed show is moved more than 2 h by the guide (a match, say)."""
        now = self.now()
        by_id = {s["id"]: s for s in self.store.all()}
        for o in self.occurrences(list(by_id.values()), now, limit=100, horizon_days=2):
            if not o.guided or o.key in self.moved_alerted or o.start - now > timedelta(hours=24):
                continue
            shown = o.start + timedelta(minutes=by_id[o.schedule_id]["margin_before"])
            if abs(shown - o.show_start) > timedelta(hours=2):
                self.moved_alerted.add(o.key)
                self.sms.alert(f"Enregistreur TV : « {o.name} » du {o.show_start.strftime('%d/%m')} est déplacé, "
                               f"enregistrement à {shown.astimezone(sch.TZ).strftime('%Hh%M')} (guide TV).")

    def convert_step(self):
        """Convert at most one finished .ts to .mkv, only while nothing records. True if one was done."""
        if self.busy(CONVERT_IDLE_MIN):
            return False
        bases = {r.base for r in list(self.running.values())}
        for ts in convert.candidates(self.out_root, bases):
            if ts in self.convert_failed:
                continue
            try:
                self.converting = ts
                convert.convert(ts, lambda: bool(self.busy(2)))
                return True
            except convert.Interrupted as e:
                log.info("conversion: %s", e)    # a recording comes: retried later
                return False
            except (convert.ConvertError, OSError) as e:
                log.warning("conversion: %s", e)
                self.convert_failed.add(ts)   # kept as .ts, retried after a restart
                rel = os.path.relpath(ts, self.out_root)
                self.sms.alert(f"Enregistreur TV : conversion en MKV ratée pour {rel} (le .ts est gardé).")
                return False
            finally:
                self.converting = None
        return False

    def recent_results(self, n=30):
        if not os.path.exists(self.results):
            return []
        with open(self.results) as f:
            lines = f.readlines()[-n:]
        out = []
        for l in reversed(lines):
            r = json.loads(l)
            f = r.get("file")
            if f and f.endswith(".ts") and not os.path.exists(f) and os.path.exists(f[:-3] + ".mkv"):
                r["file"] = f[:-3] + ".mkv"   # converted since (convert.py)
            if r.get("file") and not os.path.exists(r["file"]):
                r["deleted"] = True           # by the page, the retention rule or by hand
            out.append(r)
        return out


def host_allowed(host, ip, port, extra=()):
    """The Host header names this server: its address or name with the port it was reached
    on, localhost, or a configured extra. On port 80 browsers leave the port out."""
    names = (ip, socket.gethostname(), "localhost", "127.0.0.1")
    ok = {f"{n}:{port}".lower() for n in names} | set(extra)
    if port == 80:
        ok |= {n.lower() for n in names}
    return host.lower() in ok


def make_handler(d, extra_hosts=(), guard=None):
    extra = {h.lower() for h in extra_hosts if h}
    update_log = os.path.join(os.path.dirname(d.results), "update.log")

    def settings():
        try:
            st = os.statvfs(d.out_root)
            disk = {"free": st.f_bavail * st.f_frsize, "total": st.f_blocks * st.f_frsize}
        except OSError as e:
            disk = {"error": str(e)}
        try:
            pw = guard.enabled() if guard else None
        except ValueError:
            pw = "invalid"
        try:
            c = d.sms.config()
            sms = {"configured": bool(c), "user": c and c["user"][:2] + "****" + c["user"][-2:]}
        except notify.NotifyError as e:
            sms = {"configured": False, "error": str(e)}
        return {"sms": sms, "version": __version__, "repo": os.environ.get("FBXREC_REPO", REPO), "out": d.out_root, "disk": disk, "password": pw,
                "update_allowed": update.allowed(), "update_log": update.log_tail(update_log),
                "reboot_required": update.reboot_required(),
                "reboot_allowed": update.allowed("systemctl", "reboot")}

    class H(BaseHTTPRequestHandler):
        timeout = 30   # a client announcing more body than it sends must not pin a thread forever
        def log_message(self, fmt, *a):
            log.debug("%s " + fmt, self.address_string(), *a)

        def _head(self, code, ctype, length=None, **extra):
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            if length is not None:
                self.send_header("Content-Length", str(length))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            for k, v in extra.items():
                self.send_header(k.replace("_", "-"), v)
            self.end_headers()

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self._head(code, ctype, len(data))
            self.wfile.write(data)

        def _query(self, allowed):
            """{name: value} of the query string: each name at most once and in `allowed`, else
            None (the caller answers 400)."""
            try:
                q = urllib.parse.parse_qs(self.path.partition("?")[2], keep_blank_values=True,
                                          strict_parsing=True, max_num_fields=len(allowed))
            except ValueError:
                return None
            if set(q) - set(allowed) or any(len(v) != 1 for v in q.values()):
                return None
            return {k: v[0] for k, v in q.items()}

        def _host_ok(self):
            # blocks DNS rebinding: a foreign site resolving to this IP sends its own Host.
            # Allowed: the address and port this connection reached (follows any IP the
            # Freebox assigns), the VM's own name, localhost, plus FBXREC_HOSTS extras.
            ip, port = self.connection.getsockname()[:2]
            if not host_allowed(self.headers.get("Host", ""), ip, port, extra):
                self._send(403, {"error": "host not allowed"})
                return False
            if guard is None:
                return True
            if (self.command == "GET" and re.match(r"^/api/(running|busy\?within=\d{1,3})$", self.path)
                    and self.client_address[0] in ("127.0.0.1", "::1")):
                return True   # read-only, lets install.sh refuse to restart mid-recording or just before
            try:
                allowed = guard.allows(self.headers.get("Authorization"))
            except ValueError as e:
                log.error("%s", e)
                self._send(500, {"error": "password file unreadable, see the service log"})
                return False
            if not allowed:
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Basic realm="fbxrec", charset="UTF-8"')
                self.send_header("Content-Length", "0")
                self.end_headers()
                return False
            return True

        def _body(self):
            # JSON only: a cross-site form/text POST cannot set this type without a CORS preflight
            if self.headers.get("Content-Type", "").split(";")[0].strip() != "application/json":
                raise sch.ValidationError("Content-Type must be application/json")
            n = self.headers.get("Content-Length") or "0"
            if not n.isdigit() or not 0 < int(n) <= MAX_BODY:
                raise sch.ValidationError("body size")
            try:
                body = json.loads(self.rfile.read(int(n)))
            except ValueError:
                raise sch.ValidationError("invalid JSON") from None
            if not isinstance(body, dict):
                raise sch.ValidationError("body must be a JSON object")
            return body

        def _epg(self):
            q = self._query(("start", "hours"))
            if q is None or "start" not in q:
                return self._send(400, {"error": "start (unix time) required, hours 1 to 6"})
            start, hours = q["start"], q.get("hours", "3")
            now = int(time.time())
            if not start.isdigit() or not hours.isdigit() or not 1 <= int(hours) <= 6 \
                    or not now - 86400 <= int(start) <= now + 15 * 86400:
                return self._send(400, {"error": "start: now -1 day to +15 days, hours 1 to 6"})
            progs, missing = d.guide.window(int(start), int(hours), {c["service"] for c in d.channels})
            return self._send(200, {"progs": progs, "missing": missing})

        def _file(self):
            """A converted .mkv as is, with byte ranges: browsers play it natively (seek bar, keys)."""
            q = self._query(("file",))
            if q is None or not q.get("file", "").endswith(".mkv"):
                return self._send(400, {"error": "file: a .mkv recording"})
            try:
                path = player.resolve(d.out_root, q["file"])
            except player.NotFound:
                return self._send(404, {"error": "no such recording"})
            size = os.path.getsize(path)
            rng = self.headers.get("Range")
            a, b = 0, size - 1
            if rng is not None:
                m = re.match(r"^bytes=(\d{1,15})-(\d{0,15})$", rng.strip())
                if not m or int(m.group(1)) >= size or (m.group(2) and int(m.group(2)) < int(m.group(1))):
                    return self._head(416, "application/json", 0, Content_Range=f"bytes */{size}")
                a, b = int(m.group(1)), min(int(m.group(2) or size - 1), size - 1)
            ranged = {"Content_Range": f"bytes {a}-{b}/{size}"} if rng is not None else {}
            self._head(206 if rng is not None else 200, "video/x-matroska", b - a + 1, Accept_Ranges="bytes", **ranged)
            self.connection.settimeout(PLAY_IDLE_S)
            with open(path, "rb") as f:
                f.seek(a)
                left = b - a + 1
                try:
                    while left > 0 and (chunk := f.read(min(left, 256 * 1024))):
                        self.wfile.write(chunk)
                        left -= len(chunk)
                except OSError:   # browser seeked elsewhere or closed: normal
                    self.close_connection = True

        def _play(self, p):
            q = self._query(("file", "t"))
            if q is None:
                return self._send(400, {"error": "bad query"})
            live = [r.info.get("part") for r in list(d.running.values()) if r.is_alive() and r.info.get("part")]
            try:
                path = player.resolve(d.out_root, q.get("file"), live)
            except player.NotFound:
                return self._send(404, {"error": "no such recording"})
            if p == "/api/probe":
                return self._send(200, {"duration": player.duration(path)})
            if not path.endswith(".ts"):   # a .mkv plays as is through /api/file
                return self._send(400, {"error": "use /api/file for .mkv"})
            t = q.get("t", "0")
            if not re.match(r"^\d{1,6}(\.\d{1,3})?$", t):
                return self._send(400, {"error": "t: seconds"})
            if not player.slots.acquire(blocking=False):
                return self._send(429, {"error": f"{player.MAX_STREAMS} lectures en cours au plus"})
            try:
                self._head(200, "video/mp4", Connection="close")
                self.connection.settimeout(PLAY_IDLE_S)
                player.stream(path, float(t), self.wfile, PLAY_CLIP_S)
            finally:
                player.slots.release()
                self.close_connection = True

        def do_GET(self):
            if not self._host_ok():
                return
            p = self.path.split("?")[0]
            if p == "/":
                with open(os.path.join(STATIC, "index.html"), "rb") as f:
                    return self._send(200, f.read(), "text/html")
            if p == "/api/schedules":
                return self._send(200, d.store.all())
            if p == "/api/upcoming":
                now = d.now()
                return self._send(200, [{"key": o.key, "name": o.name, "start": o.start.isoformat(),
                                         "end": o.end.isoformat(), "service": o.service, "flavour": o.flavour,
                                         "channel": o.channel, "guided": o.guided}
                                        for o in d.occurrences(d.store.all(), now, limit=200, horizon_days=8)
                                        if o.start - now < timedelta(days=UPCOMING_DAYS)])
            if p == "/api/running":
                return self._send(200, [r.info for r in list(d.running.values()) if r.is_alive()])
            if p == "/api/results":
                return self._send(200, d.recent_results())
            if p == "/api/channels":
                return self._send(200, d.channels)
            if p == "/api/favorites":
                return self._send(200, d.favorites())
            if p == "/api/settings":
                return self._send(200, settings())
            if p in ("/api/play", "/api/probe"):
                return self._play(p)
            if p == "/api/file":
                return self._file()
            if p == "/api/busy":
                q = self._query(("within",))
                if q is None or not re.match(r"^\d{1,3}$", q.get("within", "10")):
                    return self._send(400, {"error": "within: minutes"})
                return self._send(200, {"busy": d.busy(int(q.get("within", "10")))})
            if p == "/api/retention":
                try:
                    return self._send(200, d.retention.summary())
                except retention.RetentionError as e:
                    return self._send(500, {"error": str(e)})
            if p == "/api/epg":
                return self._epg()
            if p == "/api/epg/recurrence":
                q = self._query(("service", "title", "start"))
                if q is None or not q.get("service", "").isdigit() or not q.get("start", "").isdigit() \
                        or not 0 < len(q.get("title", "")) <= 200:
                    return self._send(400, {"error": "service, title, start"})
                return self._send(200, d.guide.recurrence(int(q["service"]), q["title"], int(q["start"])))
            if p == "/api/update":
                try:
                    return self._send(200, update.latest())
                except (OSError, ValueError, KeyError) as e:
                    return self._send(502, {"error": f"vérification impossible : {e}"})
            self._send(404, {"error": "not found"})

        def do_POST(self):
            if not self._host_ok():
                return
            p = self.path.split("?")[0]
            try:
                if p == "/api/schedules":
                    s = sch.validate(self._body())
                    d.store.put(s)
                    return self._send(200, s)
                if p == "/api/settings/password" and guard is not None:
                    try:
                        auth.write_password(guard.path, self._body().get("password"))
                    except ValueError as e:
                        raise sch.ValidationError(str(e)) from None
                    return self._send(200, {"password": True})
                if p == "/api/settings/sms":
                    b = self._body()
                    cfg = {"user": b.get("user"), "key": b.get("key")}
                    try:
                        if set(b) - {"user", "key"}:
                            raise notify.NotifyError("champs attendus : user, key")
                        notify.validate(cfg["user"], cfg["key"])
                        # saved only once a real SMS went through: wrong credentials show at once
                        d.sms.send("Enregistreur TV : les alertes par SMS sont activées.", cfg)
                        d.sms.save(cfg["user"], cfg["key"])
                    except notify.NotifyError as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, {"configured": True})
                if p == "/api/settings/sms/test":
                    self._body()
                    try:
                        d.sms.send("Enregistreur TV : SMS de test.")
                    except notify.NotifyError as e:
                        return self._send(400, {"error": str(e)})
                    return self._send(200, {"sent": True})
                if p == "/api/update":
                    self._body()   # JSON required, like every write
                    why = d.busy(REBOOT_MARGIN_MIN)   # the update restarts the service (~1 min)
                    if why:
                        return self._send(409, {"error": why})
                    try:
                        update.start(update_log)
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                        return self._send(409, {"error": str(e)})
                    return self._send(202, {"started": True})
                if p == "/api/reboot":
                    self._body()
                    why = d.busy(REBOOT_MARGIN_MIN)
                    if why:
                        return self._send(409, {"error": why})
                    try:
                        update.reboot()
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                        return self._send(409, {"error": str(e)})
                    return self._send(202, {"rebooting": True})
                if p == "/api/retention":
                    b = self._body()
                    if set(b) != {"folder", "days"}:
                        raise sch.ValidationError("fields: folder, days")
                    try:
                        d.retention.set(b["folder"], b["days"])
                    except retention.RetentionError as e:
                        raise sch.ValidationError(str(e)) from None
                    return self._send(200, {"folder": b["folder"], "days": b["days"]})
                if p == "/api/favorites":
                    b = self._body()
                    if set(b) != {"services"}:
                        raise sch.ValidationError("field: services")
                    return self._send(200, d.set_favorites(b["services"]))
                if p == "/api/instant":
                    key = d.start_instant(self._body())
                    return self._send(200, {"key": key})
                if p == "/api/running/end":
                    b = self._body()
                    end = d.set_running_end(b.get("key"), b.get("end"))
                    if end is None:
                        return self._send(404, {"error": "no such recording"})
                    return self._send(200, {"end": end.isoformat()})
                if p == "/api/running/stop":
                    key = self._body().get("key")
                    r = d.running.get(key) if isinstance(key, str) else None
                    if not r:
                        return self._send(404, {"error": "no such recording"})
                    if r.info.get("instant"):   # stopped on purpose: no resume after a restart
                        d.instant.delete(r.occ.schedule_id)
                    r.stop()
                    return self._send(200, {"stopping": key})
            except sch.ValidationError as e:
                return self._send(400, {"error": str(e)})
            self._send(404, {"error": "not found"})

        def do_DELETE(self):
            if not self._host_ok():
                return
            if self.path == "/api/settings/password" and guard is not None:
                auth.remove_password(guard.path)
                return self._send(200, {"password": False})
            if self.path == "/api/settings/sms":
                d.sms.remove()
                return self._send(200, {"configured": False})
            if self.path.split("?")[0] == "/api/recording":
                q = self._query(("file",))
                if q is None or "file" not in q:
                    return self._send(400, {"error": "file"})
                try:
                    path = player.resolve(d.out_root, q["file"])   # finished .ts/.mkv only, never a part
                except player.NotFound:
                    return self._send(404, {"error": "no such recording"})
                if path.rsplit(".", 1)[0] in {r.base for r in list(d.running.values()) if r.is_alive()}:
                    return self._send(409, {"error": "enregistrement en cours"})
                if d.converting and os.path.realpath(d.converting) == path:
                    return self._send(409, {"error": "conversion en MKV en cours, réessayez dans quelques minutes"})
                os.remove(path)
                log.info("deleted from the page: %s", path)
                return self._send(200, {"deleted": True})
            m = re.match(r"^/api/schedules/([0-9a-f]{12})$", self.path)
            if not m:
                return self._send(404, {"error": "not found"})
            ok = d.store.delete(m.group(1))
            self._send(200 if ok else 404, {"deleted": ok})

    return H


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")
    data = os.environ.get("FBXREC_DATA", "/var/lib/fbxrec")
    out = os.environ.get("FBXREC_OUT")
    if not out:
        # no default: the Freebox disk name differs between models (install.sh writes it)
        raise SystemExit("FBXREC_OUT is not set (output folder, see /etc/fbxrec.env)")
    pw_file = os.environ.get("FBXREC_PASSWORD_FILE", os.path.join(data, "password"))
    port = int(os.environ.get("FBXREC_PORT", "8080"))
    hosts = os.environ.get("FBXREC_HOSTS", "").split(",")
    os.makedirs(data, exist_ok=True)
    d = Daemon(data, out)
    d.refresh_channels()
    loops = [(d.step, d.tick, "scheduler step", False), (d.watch_step, WATCH_TICK_S, "watch step", False),
             (d.guide.fetch_one, epg.FETCH_INTERVAL_S, "guide fetch", False),
             (d.refresh_channels, 86400, "playlist refresh", True)]
    if os.environ.get("FBXREC_CONVERT", "1") == "1":   # 0: keep .ts files (e.g. a consumer reads .ts)
        loops.append((d.convert_step, CONVERT_TICK_S, "conversion step", True))
    for fn, every, what, first_wait in loops:
        threading.Thread(target=forever, args=(fn, every, what, first_wait), daemon=True).start()
    guard = Guard(pw_file)
    try:
        pw_state = "on" if guard.enabled() else "off"
    except ValueError as e:   # keep recording; the web UI answers 500 until the file is fixed
        pw_state = str(e)
    log.info("listening on :%d (extra hosts %s, password %s), recording to %s", port,
             [h for h in hosts if h], pw_state, out)
    ThreadingHTTPServer(("0.0.0.0", port), make_handler(d, hosts, guard)).serve_forever()


if __name__ == "__main__":
    main()
