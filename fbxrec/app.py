"""fbxrec daemon: scheduler loop + LAN web UI/API (LAN only, optional password, see auth.py)."""
import json
import logging
import os
import re
import socket
import subprocess
import threading
import time
import urllib.request
from datetime import datetime, timedelta
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from . import REPO, __version__, auth, update
from . import schedule as sch
from .auth import Guard
from .recorder import Recording

log = logging.getLogger("fbxrec")
PLAYLIST = "http://mafreebox.freebox.fr/freeboxtv/playlist.m3u"
STATIC = os.path.join(os.path.dirname(__file__), "static")
MAX_BODY = 16 * 1024
REBOOT_MARGIN_MIN = 10   # no reboot when a recording starts this soon (a reboot takes ~1 min)


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


class Daemon:
    def __init__(self, data_dir, out_root, tick=10, now=lambda: datetime.now(sch.TZ)):
        self.store = Store(os.path.join(data_dir, "schedules.json"))
        self.results = os.path.join(data_dir, "results.jsonl")
        self.out_root, self.tick, self.now = out_root, tick, now
        self.running = {}   # occurrence key -> Recording
        self.started = set()
        self.channels, self.lock = [], threading.Lock()

    def refresh_channels(self):
        try:
            with urllib.request.urlopen(PLAYLIST, timeout=15) as r:
                self.channels = parse_playlist(r.read().decode("utf-8", "replace"))
        except OSError as e:
            log.warning("playlist unavailable: %s", e)

    def step(self):
        now = self.now()
        with self.lock:
            for k in [k for k, r in self.running.items() if not r.is_alive()]:
                del self.running[k]
            for o in sch.due(self.store.all(), now):
                if o.key not in self.started:
                    self.started.add(o.key)
                    rec = Recording(o, self.out_root, self.results, self.now)
                    self.running[o.key] = rec
                    log.info("start %s until %s", o.key, o.end.isoformat())
                    rec.start()

    def loop(self):
        while True:
            try:
                self.step()
            except Exception:
                log.exception("scheduler step failed")
            time.sleep(self.tick)

    def recent_results(self, n=30):
        if not os.path.exists(self.results):
            return []
        with open(self.results) as f:
            lines = f.readlines()[-n:]
        return [json.loads(l) for l in reversed(lines)]


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
        return {"version": __version__, "repo": os.environ.get("FBXREC_REPO", REPO), "out": d.out_root, "disk": disk, "password": pw,
                "update_allowed": update.allowed(), "update_log": update.log_tail(update_log),
                "reboot_required": update.reboot_required(),
                "reboot_allowed": update.allowed("systemctl", "reboot")}

    def busy_reason(within_min=REBOOT_MARGIN_MIN):
        """Why the VM must not reboot now (a recording runs or starts soon), else None."""
        if any(r.is_alive() for r in list(d.running.values())):
            return "enregistrement en cours, réessayez après"
        now = d.now()
        for o in sch.upcoming(d.store.all(), now, limit=1):
            if o.start - now < timedelta(minutes=within_min):
                return f"un enregistrement commence dans moins de {within_min} minutes ({o.name})"
        return None

    class H(BaseHTTPRequestHandler):
        timeout = 30   # a client announcing more body than it sends must not pin a thread forever
        def log_message(self, fmt, *a):
            log.debug("%s " + fmt, self.address_string(), *a)

        def _send(self, code, body, ctype="application/json"):
            data = body if isinstance(body, bytes) else json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype + ("; charset=utf-8" if "json" in ctype or "html" in ctype else ""))
            self.send_header("Content-Length", str(len(data)))
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(data)

        def _host_ok(self):
            # blocks DNS rebinding: a foreign site resolving to this IP sends its own Host.
            # Allowed: the address and port this connection reached (follows any IP the
            # Freebox assigns), the VM's own name, localhost, plus FBXREC_HOSTS extras.
            ip, port = self.connection.getsockname()[:2]
            names = (ip, socket.gethostname(), "localhost", "127.0.0.1")
            ok = {f"{n}:{port}".lower() for n in names} | extra
            if self.headers.get("Host", "").lower() not in ok:
                self._send(403, {"error": "host not allowed"})
                return False
            if guard is None:
                return True
            if (self.command == "GET" and self.path == "/api/running"
                    and self.client_address[0] in ("127.0.0.1", "::1")):
                return True   # read-only, lets install.sh refuse to restart mid-recording
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
                return self._send(200, [{"key": o.key, "name": o.name, "start": o.start.isoformat(),
                                         "end": o.end.isoformat(), "service": o.service, "flavour": o.flavour}
                                        for o in sch.upcoming(d.store.all(), d.now())])
            if p == "/api/running":
                return self._send(200, [r.info for r in list(d.running.values()) if r.is_alive()])
            if p == "/api/results":
                return self._send(200, d.recent_results())
            if p == "/api/channels":
                return self._send(200, d.channels)
            if p == "/api/settings":
                return self._send(200, settings())
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
                if p == "/api/update":
                    self._body()   # JSON required, like every write
                    if any(r.is_alive() for r in list(d.running.values())):
                        return self._send(409, {"error": "enregistrement en cours, réessayez après"})
                    try:
                        update.start(update_log)
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                        return self._send(409, {"error": str(e)})
                    return self._send(202, {"started": True})
                if p == "/api/reboot":
                    self._body()
                    why = busy_reason()
                    if why:
                        return self._send(409, {"error": why})
                    try:
                        update.reboot()
                    except (RuntimeError, OSError, subprocess.TimeoutExpired) as e:
                        return self._send(409, {"error": str(e)})
                    return self._send(202, {"rebooting": True})
                m = re.match(r"^/api/running/stop$", p)
                if m:
                    key = self._body().get("key")
                    r = d.running.get(key) if isinstance(key, str) else None
                    if not r:
                        return self._send(404, {"error": "no such recording"})
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
    threading.Thread(target=d.loop, daemon=True).start()

    def refresher():
        while True:
            time.sleep(86400)
            d.refresh_channels()
    threading.Thread(target=refresher, daemon=True).start()
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
