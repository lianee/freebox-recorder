import json
import os
import tempfile
import time
import unittest
from datetime import datetime, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
os.environ["FBXREC_FFMPEG"] = os.path.join(HERE, "fake_ffmpeg.py")
os.environ["FBXREC_FFPROBE"] = os.path.join(HERE, "fake_ffprobe.py")

from fbxrec import app, epg  # noqa: E402
from fbxrec import schedule as sch  # noqa: E402

TITLE = "N'oubliez pas les paroles"


def write_hours(d, progs, now):
    """progs: [(start_ts, duration_s, title)] on service 201, stored in their start hour."""
    hours = {}
    for st, du, t in progs:
        hours.setdefault(st - st % 3600, {})[str(st)] = {"start": st, "duration": du, "title": t,
                                                         "category": "", "desc": ""}
    for h, p in hours.items():
        path = os.path.join(d, f"{h}.json")
        with open(path, "w") as f:
            json.dump({"201": p}, f)
        os.utime(path, (now, now))


def fill(d, first_hour, n, now):
    """Empty hours so the guide counts as loaded there."""
    for i in range(n):
        path = os.path.join(d, f"{first_hour + i * 3600}.json")
        if not os.path.exists(path):
            with open(path, "w") as f:
                json.dump({}, f)
            os.utime(path, (now, now))


class Find(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.g = epg.Guide(self.tmp.name, lambda ts: None, now=lambda: self.now)
        self.H = (int(self.now) // 3600 + 24) * 3600 + 18 * 60   # some day, xx:18

    def tearDown(self):
        self.tmp.cleanup()

    def test_parts_merged_across_a_short_break(self):
        H = self.H
        fill(self.tmp.name, H - H % 3600 - 4 * 3600, 10, self.now)
        write_hours(self.tmp.name, [(H - 1800, 1800, "Avant"), (H + 300, 2400, "N’oubliez pas les paroles !"),
                                    (H + 2700, 300, "Météo"), (H + 3000, 2700, TITLE),
                                    (H + 5700, 1800, "Journal"), (H + 7500, 1800, TITLE)], self.now)
        self.assertEqual(self.g.find(201, TITLE, H), (H + 300, H + 5700))   # 2 parts, the third is apart

    def test_other_day_or_other_title(self):
        # (a showing later the same night IS taken since 7e1de7e: see test_moved_late_for_a_match)
        at, morning = self.evening()
        fill(self.tmp.name, morning - 3600, 60, self.now)
        write_hours(self.tmp.name, [(at + 26 * 3600, 1800, TITLE),                         # the next evening
                                    (at, 1800, "N'oubliez pas les paroles, le prime")], self.now)
        self.assertIsNone(self.g.find(201, TITLE, at))

    def evening(self):
        """A fixed 18:35 (Paris) a few days ahead, the guide loaded from 06:00 to 08:00 next day."""
        d = datetime.fromtimestamp(self.now, sch.TZ).replace(hour=18, minute=35, second=0, microsecond=0) \
            + timedelta(days=3)
        at = int(d.timestamp())
        morning = int(d.replace(hour=6, minute=0).timestamp())
        fill(self.tmp.name, morning - 3600, 28, self.now)
        return at, morning

    def test_moved_late_for_a_match(self):
        at, morning = self.evening()
        write_hours(self.tmp.name, [(at - 600, 9000, "Football : France - Espagne"),
                                    (morning + 4 * 3600, 2100, TITLE),            # 10:00 rerun
                                    (at + 6 * 3600 + 900, 2100, TITLE),           # 00:50 the show, 2 parts
                                    (at + 6 * 3600 + 3000, 2400, TITLE)], self.now)
        self.assertEqual(self.g.find(201, TITLE, at), (at + 6 * 3600 + 900, at + 6 * 3600 + 5400))

    def test_moved_earlier_that_day(self):
        at, morning = self.evening()
        write_hours(self.tmp.name, [(morning + 3 * 3600, 2100, TITLE), (at - 3 * 3600, 2100, TITLE)], self.now)
        self.assertEqual(self.g.find(201, TITLE, at), (at - 3 * 3600, at - 3 * 3600 + 2100))   # closest to usual

    def test_night_not_loaded_keeps_fixed(self):
        at, morning = self.evening()
        os.remove(os.path.join(self.tmp.name, f"{(at + 5 * 3600) - (at + 5 * 3600) % 3600}.json"))
        self.assertIsNone(self.g.find(201, TITLE, at))

    def test_guide_not_loaded(self):
        write_hours(self.tmp.name, [(self.H, 1800, TITLE)], self.now)   # neighbouring hours missing
        self.assertIsNone(self.g.find(201, TITLE, self.H))


class Recurrence(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.now = time.time()
        self.g = epg.Guide(self.tmp.name, lambda ts: None, now=lambda: self.now)
        # a Monday 18:35 Paris, 3 days ahead at least
        d = datetime.fromtimestamp(self.now, sch.TZ).replace(hour=18, minute=35, second=0, microsecond=0)
        d += timedelta(days=(7 - d.weekday()) % 7 + 7)
        self.mon = int(d.timestamp())

    def tearDown(self):
        self.tmp.cleanup()

    def test_weekdays_at_the_same_hour_only(self):
        progs = [(self.mon + k * 86400, 2100, TITLE) for k in range(6)]            # Monday to Saturday
        progs += [(self.mon + k * 86400 + 2100, 2700, TITLE) for k in range(6)]    # second part, same days
        progs += [(self.mon + 6 * 86400 + 6 * 3600, 1800, TITLE)]                  # Sunday 00:35: a night rerun
        write_hours(self.tmp.name, progs, self.now)
        r = self.g.recurrence(201, "N’oubliez pas les paroles !", self.mon)
        self.assertEqual(r, {"days": [0, 1, 2, 3, 4, 5], "count": 12})

    def test_one_off(self):
        write_hours(self.tmp.name, [(self.mon, 6000, "Nosferatu")], self.now)
        self.assertEqual(self.g.recurrence(201, "Nosferatu", self.mon), {"days": [0], "count": 1})

    def test_picture_kept_only_if_a_plain_api_image(self):
        def pic(v):
            return epg.parse({"success": True, "result": {"uuid-webtv-201": {"a": {
                "date": 1, "duration": 1, "title": "x", "picture_big": v}}}})[201][1].get("picture")
        ok = "/api/latest/tv/img/epg/programs/168x130/COL_53697718_AG.jpg"
        self.assertEqual(pic(ok), ok)
        for bad in ("http://evil.example/x.jpg", "/api/../../x.jpg", "/api/x.jpg?y", "/api/x.svg",
                    '/api/x".jpg', "javascript:alert(1)", 5, None):
            self.assertIsNone(pic(bad), bad)

    def test_series_fields_kept(self):
        out = epg.parse({"success": True, "result": {"uuid-webtv-201": {"a": {
            "date": "100", "duration": "60", "title": "Simon Coleman", "sub_title": "Le piège",
            "season_number": "2", "episode_number": 5}}}})
        p = out[201][100]
        self.assertEqual((p["sub_title"], p["season"], p["episode"]), ("Le piège", 2, 5))
        self.assertNotIn("season", epg.parse({"success": True, "result": {"uuid-webtv-201": {"a": {
            "date": 1, "duration": 1, "title": "x"}}}})[201][1])


class Follow(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        os.environ["FAKE_GROW"] = "1"
        # a clock set to tomorrow 14:00 Paris and running in real time: no midnight to skip
        base = (datetime.now(sch.TZ) + timedelta(days=1)).replace(hour=14, minute=0, second=0, microsecond=0)
        t0 = time.monotonic()
        self.clock = lambda: base + timedelta(seconds=time.monotonic() - t0)
        self.d = app.Daemon(self.tmp.name, os.path.join(self.tmp.name, "out"), now=self.clock)
        self.edir = os.path.join(self.tmp.name, "epg")
        self.fixed = (base + timedelta(minutes=30)).replace(second=0, microsecond=0)
        self.s = sch.validate({"name": "NOPLP", "service": 201, "channel": "France 2", "flavour": "sd",
                               "kind": "once", "date": self.fixed.date().isoformat(),
                               "start": self.fixed.strftime("%H:%M"),
                               "end": (self.fixed + timedelta(minutes=80)).strftime("%H:%M"),
                               "margin_before": 5, "margin_after": 2, "enabled": True,
                               "follow_guide": True, "guide_title": TITLE})
        self.d.store.put(self.s)
        self.ts = int(self.fixed.timestamp())
        fill(self.edir, self.ts - self.ts % 3600 - 4 * 3600, 12, time.time())

    def tearDown(self):
        for r in list(self.d.running.values()):
            r.stop()
            r.join(10)
        os.environ.pop("FAKE_GROW", None)
        self.tmp.cleanup()

    def guide(self, progs):
        write_hours(self.edir, progs, time.time())
        self.d.guide.found.clear()

    def test_times_from_guide_key_unchanged(self):
        self.guide([(self.ts - 40 * 60, 3600, TITLE)])           # starts 40 min before the fixed time
        o = self.d.occurrences(self.d.store.all(), self.clock())[0]
        self.assertTrue(o.guided)
        self.assertEqual(o.start, datetime.fromtimestamp(self.ts - 45 * 60, sch.UTC))    # margin 5 min
        self.assertEqual(o.end, datetime.fromtimestamp(self.ts + 22 * 60, sch.UTC))      # +2 min
        self.assertEqual(o.key, f"{self.s['id']}@{self.fixed.isoformat()}")

    def test_fallback_to_fixed_times(self):
        self.guide([(self.ts, 3600, "Autre chose")])
        o = self.d.occurrences(self.d.store.all(), self.clock())[0]
        self.assertFalse(o.guided)
        self.assertEqual(o.start, self.fixed.astimezone(sch.UTC) - timedelta(minutes=5))

    def test_starts_early_once_and_end_follows_guide_updates(self):
        self.guide([(self.ts - 33 * 60, 3600, TITLE)])           # guide: began 33 min before fixed = 3 min ago
        self.d.step()
        self.assertEqual(len(self.d.running), 1)                 # started on the guide's time, not the fixed one
        (key, rec), = self.d.running.items()
        self.guide([(self.ts - 33 * 60, 7200, TITLE)])           # the guide now says one hour longer
        self.d.step()
        self.assertEqual(list(self.d.running), [key])            # same recording, no second one
        self.assertEqual(rec.end, datetime.fromtimestamp(self.ts - 33 * 60 + 7200 + 120, sch.UTC))
        st = self.d.ends.all()
        self.assertEqual(st, [])                                 # not a manual move

    def test_manual_end_wins(self):
        self.guide([(self.ts - 33 * 60, 3600, TITLE)])
        self.d.step()
        (key, rec), = self.d.running.items()
        later = (self.clock() + timedelta(hours=3)).strftime("%H:%M")
        end = self.d.set_running_end(key, later)
        self.guide([(self.ts - 33 * 60, 7200, TITLE)])
        self.d.step()
        self.assertEqual(rec.end, end)

    def test_moved_show_recorded_at_its_new_time_and_sms_once(self):
        # the fixed slot ended 6 h ago (out of the old 4 h look-back); the guide moved the show to now
        sent = []
        self.d.sms = type("Sms", (), {"alert": lambda _, m: sent.append(m)})()
        self.d.store.delete(self.s["id"])
        fixed = self.fixed - timedelta(hours=7)
        s = sch.validate(dict({k: v for k, v in self.s.items() if k != "id"}, start=fixed.strftime("%H:%M"),
                              end=(fixed + timedelta(minutes=80)).strftime("%H:%M")))
        self.d.store.put(s)
        now = int(self.clock().timestamp())
        ft = int(fixed.timestamp())
        fill(self.edir, ft - ft % 3600 - 3 * 3600, 30, time.time())   # the guide covers that whole day
        self.guide([(ft, 7200, "Football"), (now - 60, 3600, TITLE)])
        self.d.check_moves()
        self.d.check_moves()
        self.assertEqual(len(sent), 1)
        self.assertIn("déplacé", sent[0])
        self.d.step()
        (key, rec), = self.d.running.items()
        self.assertTrue(key.startswith(s["id"] + "@"))
        self.assertEqual(rec.end, datetime.fromtimestamp(now - 60 + 3600 + 120, sch.UTC))

    def test_validation(self):
        base = {k: v for k, v in self.s.items() if k != "id"}
        for bad in ({"follow_guide": True, "guide_title": ""}, {"follow_guide": "yes"},
                    {"guide_title": "x" * 201}, {"guide_title": "a\nb"}, {"guide_title": 5}):
            with self.assertRaises(sch.ValidationError, msg=bad):
                sch.validate(dict(base, **bad))
        old = {k: v for k, v in base.items() if k not in ("follow_guide", "guide_title")}
        self.assertEqual(sch.validate(old)["follow_guide"], False)   # schedules saved before keep working


if __name__ == "__main__":
    unittest.main()
