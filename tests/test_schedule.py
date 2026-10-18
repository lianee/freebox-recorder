import unittest
from datetime import datetime, timedelta

from fbxrec import schedule as sch
from fbxrec.schedule import TZ, ValidationError

NOPLP = {"name": "NOPLP", "service": 201, "channel": "France 2", "flavour": "sd", "kind": "weekly",
         "days": [0, 1, 2, 3, 4, 5], "start": "18:40", "end": "20:00",
         "margin_before": 5, "margin_after": 2, "enabled": True}


def at(y, mo, d, h, mi):
    return datetime(y, mo, d, h, mi, tzinfo=TZ)


class Validate(unittest.TestCase):
    def test_valid(self):
        s = sch.validate(dict(NOPLP))
        self.assertRegex(s["id"], r"^[0-9a-f]{12}$")

    def bad(self, **kw):
        d = dict(NOPLP, **kw)
        with self.assertRaises(ValidationError):
            sch.validate(d)

    def test_rejects(self):
        self.bad(name="../etc")
        self.bad(name="a/b")
        self.bad(name="..")
        self.bad(name="")
        self.bad(name="x\nINJECT")
        self.bad(name=["NOPLP"])
        self.bad(service="201")          # no string to int coercion
        self.bad(service=True)           # bool is not an int here
        self.bad(service=0)
        self.bad(flavour="4k")
        self.bad(days=[])
        self.bad(days=[7])
        self.bad(days=[1, 1])
        self.bad(days=[True])
        self.bad(days="0,1")
        self.bad(start="25:00")
        self.bad(start="18:40:00")
        self.bad(end="18:40")            # same as start
        self.bad(margin_before=61)
        self.bad(margin_before=5.5)
        self.bad(enabled="yes")
        self.bad(kind="daily")
        self.bad(id="../../x")
        self.bad(extra=1)
        self.bad(channel="France\n2")
        self.bad(channel="")
        with self.assertRaises(ValidationError):
            sch.validate([NOPLP])
        with self.assertRaises(ValidationError):
            sch.validate(dict(NOPLP, kind="once"))          # no date
        with self.assertRaises(ValidationError):
            sch.validate(dict(NOPLP, kind="once", date="2026-02-30"))
        with self.assertRaises(ValidationError):
            sch.validate(dict(NOPLP, kind="once", date="20261031"))


class Occurrences(unittest.TestCase):
    def setUp(self):
        self.s = sch.validate(dict(NOPLP))

    def test_margins(self):
        o = sch.occurrence_on(self.s, at(2026, 10, 15, 0, 0).date())
        self.assertEqual((o.start, o.end), (at(2026, 10, 15, 18, 35), at(2026, 10, 15, 20, 2)))
        self.assertEqual(o.start.astimezone(TZ).strftime("%H:%M"), "18:35")

    def test_sunday_excluded(self):
        self.assertIsNone(sch.occurrence_on(self.s, at(2026, 10, 18, 0, 0).date()))

    def test_due_window(self):
        self.assertEqual(sch.due([self.s], at(2026, 10, 15, 18, 34)), [])
        self.assertEqual(len(sch.due([self.s], at(2026, 10, 15, 18, 35))), 1)
        self.assertEqual(len(sch.due([self.s], at(2026, 10, 15, 20, 1))), 1)
        self.assertEqual(sch.due([self.s], at(2026, 10, 15, 20, 2)), [])

    def test_disabled(self):
        self.assertEqual(sch.upcoming([dict(self.s, enabled=False)], at(2026, 10, 15, 12, 0)), [])

    def test_upcoming_skips_finished_and_sunday(self):
        up = sch.upcoming([self.s], at(2026, 10, 17, 21, 0), limit=2)  # Saturday evening
        self.assertEqual([o.show_start.date().isoformat() for o in up], ["2026-10-19", "2026-10-20"])

    def test_overnight_once(self):
        p = sch.validate(dict(NOPLP, kind="once", date="2026-10-31", start="21:00", end="00:30",
                              margin_before=5, margin_after=15))
        p.pop("days", None)
        o = sch.occurrence_on(p, at(2026, 10, 31, 0, 0).date())
        self.assertEqual(o.end, at(2026, 11, 1, 0, 45))
        self.assertEqual(len(sch.due([p], at(2026, 11, 1, 0, 10))), 1)  # still due after midnight
        self.assertIsNone(sch.occurrence_on(p, at(2026, 11, 7, 0, 0).date()))

    def test_dst_end(self):
        # night of 24-25 Oct 2026: 03:00 -> 02:00, a 01:00-04:00 show lasts 4 real hours
        p = sch.validate(dict(NOPLP, kind="once", date="2026-10-25", start="01:00", end="04:00",
                              margin_before=0, margin_after=0))
        o = sch.occurrence_on(p, at(2026, 10, 25, 0, 0).date())
        self.assertEqual(o.end - o.start, timedelta(hours=4))

    def test_dst_start(self):
        # 29 Mar 2026: 02:00 -> 03:00, a 01:00-04:00 show lasts 2 real hours
        p = sch.validate(dict(NOPLP, kind="once", date="2026-03-29", start="01:00", end="04:00",
                              margin_before=0, margin_after=0))
        o = sch.occurrence_on(p, at(2026, 3, 29, 0, 0).date())
        self.assertEqual(o.end.timestamp() - o.start.timestamp(), 7200)

    def test_key_stable(self):
        a = sch.occurrence_on(self.s, at(2026, 10, 15, 0, 0).date())
        b = sch.occurrence_on(self.s, at(2026, 10, 15, 0, 0).date())
        self.assertEqual(a.key, b.key)


if __name__ == "__main__":
    unittest.main()
