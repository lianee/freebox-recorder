import http.client
import json
import os
import socket
import tempfile
import threading
import time
import unittest
from datetime import datetime, timedelta
from http.server import ThreadingHTTPServer

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["FBXREC_FFMPEG"] = os.path.join(HERE, "fake_ffmpeg.py")
os.environ["FBXREC_FFPROBE"] = os.path.join(HERE, "fake_ffprobe.py")

from fbxrec import app  # noqa: E402
from fbxrec.schedule import TZ  # noqa: E402

PLAYLIST = """#EXTM3U
#EXTINF:0,2 - France 2 (auto)
rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service=201
#EXTINF:0,2 - France 2 (HD)
rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service=201&flavour=hd
#EXTINF:0,2 - France 2 (standard)
rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service=201&flavour=sd
#EXTINF:0,1 - TF1 (HD)
rtsp://mafreebox.freebox.fr/fbxtv_pub/stream?namespace=1&service=612&flavour=hd
"""

NOPLP = {"name": "NOPLP", "service": 201, "channel": "France 2", "flavour": "sd", "kind": "weekly",
         "days": [0, 1, 2, 3, 4, 5], "start": "18:40", "end": "20:00",
         "margin_before": 5, "margin_after": 2, "enabled": True}


class Playlist(unittest.TestCase):
    def test_parse(self):
        ch = app.parse_playlist(PLAYLIST)
        self.assertEqual([(c["number"], c["name"], c["service"]) for c in ch],
                         [(1, "TF1", 612), (2, "France 2", 201)])
        self.assertEqual(ch[1]["flavours"], ["hd", "sd"])


class Api(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory()
        cls.d = app.Daemon(cls.tmp.name, os.path.join(cls.tmp.name, "out"))
        cls.d.channels = app.parse_playlist(PLAYLIST)
        cls.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(cls.d, {"good.host:1"}))
        threading.Thread(target=cls.srv.serve_forever, daemon=True).start()
        cls.port = cls.srv.server_address[1]

    @classmethod
    def tearDownClass(cls):
        cls.srv.shutdown()
        cls.tmp.cleanup()

    def req(self, method, path, body=None, host="good.host:1", ctype="application/json", raw=None, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        data = raw if raw is not None else (json.dumps(body).encode() if body is not None else None)
        h = {"Host": host}
        if data is not None:
            h["Content-Type"] = ctype
        h.update(headers or {})
        c.request(method, path, body=data, headers=h)
        r = c.getresponse()
        out = r.read()
        c.close()
        try:
            return r.status, json.loads(out)
        except ValueError:
            return r.status, out

    def test_favorites(self):
        self.assertEqual(self.req("GET", "/api/favorites"), (200, []))
        self.assertEqual(self.req("POST", "/api/favorites", {"services": [203, 201, 201]}), (200, [201, 203]))
        self.assertEqual(self.req("GET", "/api/favorites"), (200, [201, 203]))   # kept on the VM
        for body in ({"services": ["201"]}, {"services": [True]}, {"services": [0]}, {"services": 201},
                     {"services": [201], "x": 1}, {}, {"services": list(range(1, 1100))}):
            self.assertEqual(self.req("POST", "/api/favorites", body)[0], 400, str(body)[:40])
        self.assertEqual(self.req("POST", "/api/favorites", {"services": [1]}, host="evil.example:1")[0], 403)
        self.assertEqual(self.req("GET", "/api/favorites")[1], [201, 203])
        self.req("POST", "/api/favorites", {"services": []})

    def test_upcoming_week_with_channel(self):
        now = datetime.now(TZ)
        daily = dict(NOPLP, name="Tous les jours", days=list(range(7)))
        ids = []
        for extra in (daily, {**{k: v for k, v in NOPLP.items() if k != "days"}, "name": "Dans 6 jours",
                              "kind": "once", "date": (now + timedelta(days=6)).date().isoformat()},
                      {**{k: v for k, v in NOPLP.items() if k != "days"}, "name": "Dans 9 jours",
                       "kind": "once", "date": (now + timedelta(days=9)).date().isoformat()}):
            st, s = self.req("POST", "/api/schedules", extra)
            ids.append(s["id"])
        try:
            st, up = self.req("GET", "/api/upcoming")
            names = [o["name"] for o in up]
            self.assertIn("Dans 6 jours", names)        # not hidden behind the daily ones
            self.assertNotIn("Dans 9 jours", names)     # beyond the week
            self.assertGreaterEqual(names.count("Tous les jours"), 7)   # the 7th day was lost once
            self.assertEqual({o["channel"] for o in up}, {"France 2"})
        finally:
            for i in ids:
                self.req("DELETE", "/api/schedules/" + i)

    # positive path: create, read back, edit, toggle, delete
    def test_crud(self):
        st, s = self.req("POST", "/api/schedules", NOPLP)
        self.assertEqual(st, 200)
        st, items = self.req("GET", "/api/schedules")
        self.assertIn(s["id"], [x["id"] for x in items])
        st, s2 = self.req("POST", "/api/schedules", dict(s, end="20:05"))
        self.assertEqual((st, s2["id"]), (200, s["id"]))
        with open(os.path.join(self.tmp.name, "schedules.json")) as f:
            stored = [x for x in json.load(f) if x["id"] == s["id"]]
        self.assertEqual([x["end"] for x in stored], ["20:05"])      # replaced, not duplicated
        st, up = self.req("GET", "/api/upcoming")
        self.assertTrue(any(o["name"] == "NOPLP" for o in up))
        st, r = self.req("DELETE", "/api/schedules/" + s["id"])
        self.assertEqual((st, r["deleted"]), (200, True))
        st, items = self.req("GET", "/api/schedules")
        self.assertNotIn(s["id"], [x["id"] for x in items])

    def test_index_and_channels(self):
        st, body = self.req("GET", "/")
        self.assertEqual(st, 200)
        self.assertIn(b"Enregistreur TV", body)
        st, ch = self.req("GET", "/api/channels")
        self.assertEqual(len(ch), 2)

    # attacks
    def test_foreign_host_refused(self):
        for m, p in (("GET", "/api/schedules"), ("POST", "/api/schedules"), ("DELETE", "/api/schedules/" + "a" * 12)):
            st, _ = self.req(m, p, NOPLP if m == "POST" else None, host="evil.example")
            self.assertEqual(st, 403, (m, p))
        st, _ = self.req("GET", "/", host="")
        self.assertEqual(st, 403)
        # right address, wrong port; name that only starts like an allowed one
        for h in (f"127.0.0.1:{self.port + 1}", f"127.0.0.1:{self.port}.evil.example", "127.0.0.1"):
            st, _ = self.req("GET", "/api/schedules", host=h)
            self.assertEqual(st, 403, h)

    def test_own_addresses_allowed(self):
        # no IP configured anywhere: the address the connection reached, the VM name, localhost
        for h in (f"127.0.0.1:{self.port}", f"localhost:{self.port}", f"{socket.gethostname()}:{self.port}",
                  f"LOCALHOST:{self.port}", "GOOD.HOST:1"):
            st, _ = self.req("GET", "/api/schedules", host=h)
            self.assertEqual(st, 200, h)

    def test_simple_cross_site_post_refused(self):
        # what a hostile web page can send without a CORS preflight
        for ct in ("text/plain", "application/x-www-form-urlencoded", "multipart/form-data", "application/json-x"):
            st, _ = self.req("POST", "/api/schedules", NOPLP, ctype=ct)
            self.assertEqual(st, 400, ct)

    def test_malformed_bodies(self):
        self.assertEqual(self.req("POST", "/api/schedules", [NOPLP])[0], 400)
        self.assertEqual(self.req("POST", "/api/schedules", raw=b"{not json")[0], 400)
        self.assertEqual(self.req("POST", "/api/schedules", raw=b"x" * (app.MAX_BODY + 1))[0], 400)
        self.assertEqual(self.req("POST", "/api/schedules", raw=b"{}", headers={"Content-Length": "abc"})[0], 400)
        self.assertEqual(self.req("POST", "/api/schedules", dict(NOPLP, name="../../etc"))[0], 400)
        self.assertEqual(self.req("POST", "/api/running/stop", ["x"])[0], 400)
        self.assertEqual(self.req("POST", "/api/running/stop", {"key": ["x"]})[0], 404)

    def test_delete_traversal(self):
        for p in ("/api/schedules/../schedules.json", "/api/schedules/%2e%2e", "/api/schedules/" + "a" * 13):
            self.assertEqual(self.req("DELETE", p)[0], 404, p)

    def test_server_survives(self):
        self.req("POST", "/api/schedules", raw=b"\xff\xfe")
        self.assertEqual(self.req("GET", "/api/schedules")[0], 200)


class HostCheck(unittest.TestCase):
    def test_port_80_without_port_in_host(self):
        ip = "192.168.1.22"
        self.assertTrue(app.host_allowed("192.168.1.22", ip, 80))         # what a browser sends on :80
        self.assertTrue(app.host_allowed("192.168.1.22:80", ip, 80))
        self.assertFalse(app.host_allowed("192.168.1.22", ip, 8080))      # elsewhere the port is required
        self.assertTrue(app.host_allowed("192.168.1.22:8080", ip, 8080))
        for evil in ("evil.example", "evil.example:80", "192.168.1.22.evil.example", "", "192.168.1.23"):
            self.assertFalse(app.host_allowed(evil, ip, 80), evil)


class DaemonStep(unittest.TestCase):
    def test_starts_once_and_records(self):
        with tempfile.TemporaryDirectory() as tmp:
            now = datetime.now(TZ)
            d = app.Daemon(tmp, os.path.join(tmp, "out"))
            s = dict(NOPLP, kind="once", date=now.date().isoformat(), margin_before=0, margin_after=0,
                     start=(now - timedelta(minutes=1)).strftime("%H:%M"),
                     # +3 min: times are cut to the minute, at hh:mm:59 a +1 min end was 1 s away
                     # and nothing got recorded (proven 2026-10-20)
                     end=(now + timedelta(minutes=3)).strftime("%H:%M"))
            s.pop("days")
            if s["end"] < s["start"]:
                self.skipTest("too close to midnight")
            from fbxrec import schedule as sch
            d.store.put(sch.validate(s))
            d.step()
            d.step()
            self.assertEqual(len(d.running), 1)          # not started twice
            rec = next(iter(d.running.values()))
            # stop once data has arrived: a fixed 1 s sleep lost the race against a slow
            # ffmpeg start on a loaded machine (state "failed", nothing recorded, 2026-10-20)
            part, deadline = rec.base + ".part0.ts", time.monotonic() + 15
            while not (os.path.exists(part) and os.path.getsize(part)) and time.monotonic() < deadline:
                time.sleep(0.05)
            rec.stop()
            rec.join(10)
            with open(d.results) as f:
                info = json.loads(f.readline())
            self.assertEqual(info["state"], "stopped")
            self.assertTrue(os.path.getsize(info["file"]) > 0)



def wait_data(rec, timeout=15):
    part, deadline = rec.base + ".part0.ts", time.monotonic() + timeout
    while not (os.path.exists(part) and os.path.getsize(part)) and time.monotonic() < deadline:
        time.sleep(0.05)


class Instant(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        # a clock set to tomorrow 14:00 Paris, running in real time: no midnight to skip
        base = (datetime.now(TZ) + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        t0 = time.monotonic()
        self.clock = lambda: base + timedelta(seconds=time.monotonic() - t0)
        self.d = self.daemon()
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.d, {"good.host:1"}))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        for r in list(self.d.running.values()):
            r.stop()
            r.join(10)
        self.tmp.cleanup()

    def daemon(self):
        d = app.Daemon(self.tmp.name, os.path.join(self.tmp.name, "out"), now=self.clock)
        d.channels = app.parse_playlist(PLAYLIST)
        return d

    req = Api.req

    def stored(self):
        with open(os.path.join(self.tmp.name, "instant.json")) as f:
            return json.load(f)

    def test_start_change_end_stop(self):
        st, r = self.req("POST", "/api/instant", {"service": 201})
        self.assertEqual(st, 200)
        key = r["key"]
        st, run = self.req("GET", "/api/running")
        info = next(x for x in run if x["key"] == key)
        self.assertEqual((info["instant"], info["name"], info["channel"]), (True, app.INSTANT_NAME, "France 2"))
        end = datetime.fromisoformat(info["end"]) - self.clock()
        self.assertTrue(timedelta(minutes=59) < end <= timedelta(minutes=60))   # default 1 h
        # move the end: next occurrence of HH:MM, kept on disk and applied to the running capture
        later = (self.clock() + timedelta(hours=2)).strftime("%H:%M")
        st, r = self.req("POST", "/api/running/end", {"key": key, "end": later})
        self.assertEqual(st, 200)
        new_end = datetime.fromisoformat(r["end"])
        self.assertEqual(new_end.astimezone(TZ).strftime("%H:%M"), later)
        self.assertTrue(timedelta(hours=1, minutes=58) < new_end - self.clock() <= timedelta(hours=2))
        self.assertEqual([datetime.fromisoformat(i["end"]) for i in self.stored()], [new_end])
        self.assertEqual(self.d.running[key].end, new_end)
        wait_data(self.d.running[key])
        st, _ = self.req("POST", "/api/running/stop", {"key": key})
        self.assertEqual(st, 200)
        self.assertEqual(self.stored(), [])       # stopped on purpose: no resume
        self.d.running[key].join(10)
        with open(self.d.results) as f:
            res = json.loads(f.readlines()[-1])
        self.assertEqual(res["state"], "stopped")
        self.assertEqual(os.path.dirname(res["file"]), os.path.join(self.tmp.name, "out", app.INSTANT_NAME))

    def test_resumes_after_restart(self):
        key = self.d.start_instant({"service": 201, "minutes": 30})
        first = self.d.running[key]
        wait_data(first)
        first.stop()        # the process dies; the store keeps the instant recording
        first.join(10)
        self.assertEqual(len(self.stored()), 1)
        d2 = self.daemon()
        d2.step()
        try:
            self.assertIn(key, d2.running)
            self.assertTrue(d2.running[key].base.endswith(" - France 2 (2)"))   # first part kept
        finally:
            d2.running[key].stop()
            d2.running[key].join(10)

    def test_expired_is_dropped(self):
        now = self.clock().astimezone(app.sch.UTC).replace(microsecond=0)
        self.d.instant.put({"id": "b" * 12, "service": 201, "channel": "France 2", "flavour": "sd",
                            "start": (now - timedelta(hours=2)).isoformat(),
                            "end": (now - timedelta(hours=1)).isoformat()})
        self.d.step()
        self.assertEqual((self.stored(), self.d.running), ([], {}))

    def test_rejects(self):
        for body in ({"service": 999}, {"service": "201"}, {"service": True}, {"service": 201, "minutes": 0},
                     {"service": 201, "minutes": 1441}, {"service": 201, "minutes": "60"},
                     {"service": 201, "flavour": "4k"}, {"service": 201, "name": "x"}, {}):
            st, _ = self.req("POST", "/api/instant", body)
            self.assertEqual(st, 400, body)
        st, _ = self.req("POST", "/api/instant", raw=b"service=201", ctype="text/plain")
        self.assertEqual(st, 400)
        st, _ = self.req("POST", "/api/instant", {"service": 201}, host="evil.example:1")
        self.assertNotEqual(st, 200)
        self.assertEqual(self.d.running, {})
        st, r = self.req("POST", "/api/instant", {"service": 201})
        for end in ("25:00", "9:00", 900, None):
            st, _ = self.req("POST", "/api/running/end", {"key": r["key"], "end": end})
            self.assertEqual(st, 400, end)
        st, _ = self.req("POST", "/api/running/end", {"key": "nope", "end": "10:00"})
        self.assertEqual(st, 404)


    def scheduled(self, now):
        """A once schedule running now, from 1 min ago to 3 min ahead (cut to the minute: at least 2 min)."""
        s = dict(NOPLP, kind="once", date=now.date().isoformat(), margin_before=0, margin_after=0,
                 start=(now - timedelta(minutes=1)).strftime("%H:%M"),
                 end=(now + timedelta(minutes=3)).strftime("%H:%M"))
        del s["days"]
        s = app.sch.validate(s)
        self.d.store.put(s)
        return s

    def test_scheduled_end_moved_and_kept_after_restart(self):
        now = self.clock()
        self.scheduled(now)
        self.d.step()
        key, rec = next(iter(self.d.running.items()))
        later = (now + timedelta(hours=2)).strftime("%H:%M")
        st, r = self.req("POST", "/api/running/end", {"key": key, "end": later})
        self.assertEqual(st, 200)
        self.assertEqual(rec.end, datetime.fromisoformat(r["end"]))
        with open(os.path.join(self.tmp.name, "end_overrides.json")) as f:
            self.assertEqual([x["id"] for x in json.load(f)], [key])
        wait_data(rec)
        rec.stop()
        rec.join(10)
        # restart 10 min later: the slot (3 min) is over, the moved end is not
        d2 = app.Daemon(self.tmp.name, os.path.join(self.tmp.name, "out"), now=lambda: self.clock() + timedelta(minutes=10))
        d2.step()
        try:
            self.assertIn(key, d2.running)
            self.assertEqual(d2.running[key].end, datetime.fromisoformat(r["end"]))
        finally:
            for x in d2.running.values():
                x.stop()
                x.join(10)

    def test_moved_end_forgotten_once_past(self):
        now = self.clock()
        self.d.ends.put({"id": "a" * 12 + "@x", "end": (now - timedelta(minutes=1)).astimezone(app.sch.UTC).isoformat()})
        self.d.step()
        self.assertEqual(self.d.ends.all(), [])

    def test_limit(self):
        for _ in range(app.INSTANT_MAX):
            self.d.start_instant({"service": 201})
        st, _ = self.req("POST", "/api/instant", {"service": 201})
        self.assertEqual(st, 400)
        self.assertEqual(len(self.d.running), app.INSTANT_MAX)



class Player(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.out = os.path.join(self.tmp.name, "out")
        os.makedirs(os.path.join(self.out, "NOPLP"))
        self.rel = "NOPLP/2026-10-19 18h40 - France 2.ts"
        with open(os.path.join(self.out, self.rel), "wb") as f:
            f.write(b"G" * 188)
        with open(os.path.join(self.tmp.name, "secret.ts"), "wb") as f:
            f.write(b"x")
        os.symlink(os.path.join(self.tmp.name, "secret.ts"), os.path.join(self.out, "NOPLP", "link.ts"))
        self.d = app.Daemon(self.tmp.name, self.out)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.d, {"good.host:1"}))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]
        self.log = os.path.join(self.tmp.name, "cmd.log")
        os.environ["FAKE_LOG"] = self.log

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    req = Api.req

    def q(self, path, **params):
        from urllib.parse import urlencode
        return path + "?" + urlencode(params)

    def test_probe_and_stream(self):
        st, r = self.req("GET", self.q("/api/probe", file=self.rel))
        self.assertEqual((st, r), (200, {"duration": 5400.5}))
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", self.q("/api/play", file=self.rel, t="2700"), headers={"Host": "good.host:1"})
        resp = c.getresponse()
        body = resp.read()
        c.close()
        self.assertEqual((resp.status, resp.getheader("Content-Type"), body), (200, "video/mp4", b"MP4" * 1000))
        with open(self.log) as f:
            cmd = json.loads(f.readlines()[-1])
        self.assertEqual(cmd[cmd.index("-ss") + 1], "2700.000")
        self.assertEqual(cmd[cmd.index("-i") + 1], os.path.join(os.path.realpath(self.out), self.rel))
        self.assertIn("aac_adtstoasc", cmd)
        self.assertEqual(cmd[cmd.index("-readrate") + 1], "2")   # paced: ~1 % CPU instead of 30-50 %
        self.assertIn("delay_moov", cmd[cmd.index("-movflags") + 1])   # HE-AAC config in the header
        self.assertEqual(app.player.slots._value, app.player.MAX_STREAMS)   # slot given back

    def test_rejected_files(self):
        for rel in ("../secret.ts", "NOPLP/../../secret.ts", "/etc/passwd", self.out + "/" + self.rel,
                    "NOPLP/link.ts", "NOPLP/missing.ts", "NOPLP", "NOPLP/x.part0.ts", "NOPLP/a.txt",
                    "NOPLP/a\x00.ts", ""):
            st, _ = self.req("GET", self.q("/api/probe", file=rel))
            self.assertEqual(st, 404, rel)
            st, _ = self.req("GET", self.q("/api/play", file=rel, t="0"))
            self.assertEqual(st, 404, rel)

    def test_bad_queries(self):
        base = "/api/play?file=" + self.rel.replace(" ", "%20")
        for path in (base + "&t=-5", base + "&t=1e3", base + "&t=abc", base + "&t=1&t=2",
                     base + "&file=x", base + "&t=1&x=1", "/api/play?" + "&".join(["a=1"] * 50), "/api/play?a"):
            st, _ = self.req("GET", path)
            self.assertEqual(st, 400, path)
        st, _ = self.req("GET", self.q("/api/play", file=self.rel, t="0"), host="evil.example:1")
        self.assertEqual(st, 403)

    def raw(self, path, headers=None):
        c = http.client.HTTPConnection("127.0.0.1", self.port, timeout=5)
        c.request("GET", path, headers=dict({"Host": "good.host:1"}, **(headers or {})))
        r = c.getresponse()
        body = r.read()
        c.close()
        return r, body

    def test_mkv_direct_with_ranges(self):
        rel = "NOPLP/2026-10-19 18h40 - France 2.mkv"
        data = bytes(range(256)) * 40
        with open(os.path.join(self.out, rel), "wb") as f:
            f.write(data)
        url = self.q("/api/file", file=rel)
        r, body = self.raw(url)
        self.assertEqual((r.status, r.getheader("Accept-Ranges"), body), (200, "bytes", data))
        self.assertEqual(r.getheader("Content-Type"), "video/x-matroska")
        r, body = self.raw(url, {"Range": "bytes=100-199"})
        self.assertEqual((r.status, body, r.getheader("Content-Range")), (206, data[100:200], f"bytes 100-199/{len(data)}"))
        r, body = self.raw(url, {"Range": "bytes=10000-"})
        self.assertEqual((r.status, body), (206, data[10000:]))
        r, body = self.raw(url, {"Range": f"bytes=10000-{10 ** 12}"})     # end past the file: clamped
        self.assertEqual((r.status, body), (206, data[10000:]))
        for bad in (f"bytes={len(data)}-", "bytes=50-10", "bytes=-500", "bytes=0-1,5-9", "items=0-1", "bytes=a-"):
            r, _ = self.raw(url, {"Range": bad})
            self.assertEqual(r.status, 416, bad)
        st, _ = self.req("GET", self.q("/api/file", file=self.rel))          # .ts: not served raw
        self.assertEqual(st, 400)
        st, _ = self.req("GET", self.q("/api/play", file=rel, t="0"))        # .mkv: no remux
        self.assertEqual(st, 400)
        for evil in ("../x.mkv", "/etc/x.mkv", "NOPLP/../../x.mkv"):
            st, _ = self.req("GET", self.q("/api/file", file=evil))
            self.assertEqual(st, 404, evil)
        st, _ = self.req("GET", url + "&file=x.mkv")
        self.assertEqual(st, 400)
        st, _ = self.req("GET", url, host="evil.example:1")
        self.assertEqual(st, 403)

    def test_live_part_only_while_recording(self):
        rel = "NOPLP/2026-10-20 18h40 - France 2.part0.ts"
        part = os.path.join(self.out, rel)
        with open(part, "wb") as f:
            f.write(b"G" * 188)

        class Live:
            info = {"part": part}
            alive = True
            def is_alive(self): return self.alive
        st, _ = self.req("GET", self.q("/api/probe", file=rel))
        self.assertEqual(st, 404)                    # nobody records it
        rec = Live()
        self.d.running["k"] = rec
        st, r = self.req("GET", self.q("/api/probe", file=rel))
        self.assertEqual((st, r), (200, {"duration": 5400.5}))
        st, _ = self.req("GET", self.q("/api/probe", file="NOPLP/other.part0.ts"))
        self.assertEqual(st, 404)                    # another part: still refused
        rec.alive = False
        st, _ = self.req("GET", self.q("/api/probe", file=rel))
        self.assertEqual(st, 404)                    # finished: refused again
        del self.d.running["k"]

    def test_delete_recording(self):
        mkv = os.path.join(self.out, "NOPLP", "2026-10-19 18h40 - France 2.mkv")
        with open(mkv, "wb") as f:
            f.write(b"M")
        rel = "NOPLP/2026-10-19 18h40 - France 2.mkv"
        st, _ = self.req("DELETE", self.q("/api/recording", file=rel), host="evil.example:1")
        self.assertEqual(st, 403)
        for evil in ("../secret.ts", "NOPLP/link.ts", "NOPLP/x.part0.ts", "/etc/passwd"):
            st, _ = self.req("DELETE", self.q("/api/recording", file=evil))
            self.assertEqual(st, 404, evil)
        self.assertEqual(self.req("DELETE", "/api/recording")[0], 400)

        class Rec:   # the .ts is being recorded
            base = os.path.join(os.path.realpath(self.out), self.rel)[:-3]
            def is_alive(self): return True
        self.d.running["k"] = Rec()
        st, _ = self.req("DELETE", self.q("/api/recording", file=self.rel))
        self.assertEqual(st, 409)
        del self.d.running["k"]
        self.d.converting = os.path.join(self.out, self.rel)
        st, r = self.req("DELETE", self.q("/api/recording", file=self.rel))
        self.assertEqual(st, 409)
        self.assertIn("conversion", r["error"])
        self.d.converting = None
        self.assertTrue(os.path.exists(os.path.join(self.out, self.rel)))
        st, r = self.req("DELETE", self.q("/api/recording", file=rel))
        self.assertEqual((st, r), (200, {"deleted": True}))
        self.assertFalse(os.path.exists(mkv))
        with open(self.d.results, "w") as f:
            f.write(json.dumps({"key": "k", "file": mkv}) + "\n")
        self.assertTrue(self.d.recent_results()[0]["deleted"])

    def test_busy(self):
        for _ in range(app.player.MAX_STREAMS):
            app.player.slots.acquire()
        try:
            st, _ = self.req("GET", self.q("/api/play", file=self.rel, t="0"))
            self.assertEqual(st, 429)
        finally:
            for _ in range(app.player.MAX_STREAMS):
                app.player.slots.release()



class Guide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.d = app.Daemon(self.tmp.name, os.path.join(self.tmp.name, "out"))
        self.d.channels = app.parse_playlist(PLAYLIST)
        self.calls = []

        def fetch(ts):
            self.calls.append(ts)
            return {"success": True, "result": {"uuid-webtv-201": {"a": {"date": ts, "duration": 3600, "title": "F2"}},
                                                "uuid-webtv-999": {"a": {"date": ts, "duration": 3600, "title": "x"}}}}
        self.d.guide = app.epg.Guide(os.path.join(self.tmp.name, "epg2"), fetch)
        self.srv = ThreadingHTTPServer(("127.0.0.1", 0), app.make_handler(self.d, {"good.host:1"}))
        threading.Thread(target=self.srv.serve_forever, daemon=True).start()
        self.port = self.srv.server_address[1]

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        self.tmp.cleanup()

    req = Api.req

    def test_window(self):
        now = int(time.time())
        st, r = self.req("GET", f"/api/epg?start={now}&hours=2")
        self.assertEqual((st, r["progs"], len(r["missing"])), (200, {}, 2))   # nothing asked to the box yet
        self.assertEqual(self.calls, [])
        self.d.guide.fetch_one()
        self.d.guide.fetch_one()
        st, r = self.req("GET", f"/api/epg?start={now}&hours=2")
        self.assertEqual(r["missing"], [])
        self.assertEqual(list(r["progs"]), ["201"])      # channels outside the playlist dropped
        self.assertEqual([p["title"] for p in r["progs"]["201"]], ["F2", "F2"])

    def test_bad_queries(self):
        now = int(time.time())
        for q in ("", "?hours=3", f"?start={now}&hours=7", f"?start={now}&hours=0", f"?start=-{now}",
                  f"?start={now + 16 * 86400}", f"?start={now - 2 * 86400}", f"?start={now}&start={now}",
                  f"?start={now}&x=1", "?start=1e9", f"?start={now}&hours=3&a=1"):
            st, _ = self.req("GET", "/api/epg" + q)
            self.assertEqual(st, 400, q)
        self.assertEqual(self.calls, [])
        st, _ = self.req("GET", f"/api/epg?start={now}", host="evil.example:1")
        self.assertEqual(st, 403)

    def test_recurrence_route(self):
        st, r = self.req("GET", "/api/epg/recurrence?service=201&title=Journal%2020h00&start=" + str(int(time.time())))
        self.assertEqual((st, r), (200, {"days": [], "count": 0}))     # nothing cached: no showing found
        for q in ("service=x&title=a&start=1", "service=201&title=&start=1", "service=201&title=a",
                  "service=201&title=" + "a" * 201 + "&start=1", "service=201&title=a&start=1&x=1",
                  "service=201&service=202&title=a&start=1"):
            st, _ = self.req("GET", "/api/epg/recurrence?" + q)
            self.assertEqual(st, 400, q)
        self.assertEqual(self.calls, [])                                # never asks the box

    def test_box_down(self):
        def fetch(ts):
            raise OSError("down")
        self.d.guide = app.epg.Guide(os.path.join(self.tmp.name, "epg3"), fetch)
        self.assertEqual(self.d.guide.fetch_one(), 120)
        st, r = self.req("GET", f"/api/epg?start={int(time.time())}")
        self.assertEqual((st, len(r["missing"])), (200, 3))


if __name__ == "__main__":
    unittest.main()
