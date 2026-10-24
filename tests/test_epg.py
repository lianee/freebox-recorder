import os
import tempfile
import unittest
import urllib.error

from fbxrec import epg


def answer(progs):
    return {"success": True, "result": progs}


H = 1792497600   # a whole hour


class Parse(unittest.TestCase):
    def test_mixed_number_types(self):
        # the Freebox sends 300 and "300" alike (seen 2026-10-20)
        out = epg.parse(answer({"uuid-webtv-201": {
            "a": {"date": str(H), "duration": "300", "title": "Météo", "desc": "x" * 1000},
            "b": {"date": H + 300, "duration": 3300, "title": "Journal", "category_name": "Info"}}}))
        self.assertEqual(sorted(out[201]), [H, H + 300])
        self.assertEqual(out[201][H]["duration"], 300)
        self.assertEqual(len(out[201][H]["desc"]), epg.DESC_MAX)
        self.assertEqual(out[201][H + 300]["category"], "Info")

    def test_rejects_bad_numbers(self):
        for bad in ("12a", None, True, 1.5, -3):
            with self.assertRaises(epg.EpgError, msg=bad):
                epg.parse(answer({"uuid-webtv-201": {"a": {"date": bad, "duration": 60, "title": "t"}}}))
        with self.assertRaises(epg.EpgError):
            epg.parse({"success": False})

    def test_ignores_other_ids(self):
        out = epg.parse(answer({"uuid-other-5": {"a": {"date": H, "duration": 60}}, "uuid-webtv-x": {}}))
        self.assertEqual(out, {})


class FakeBox:
    """by_time answers: one programme per hour per service; `status` makes it refuse."""
    def __init__(self):
        self.calls, self.status = [], None

    def __call__(self, ts):
        self.calls.append(ts)
        if self.status:
            raise urllib.error.HTTPError("u", self.status, "refused", {}, None)
        return answer({"uuid-webtv-201": {"a": {"date": H + 3000, "duration": 1200, "title": "Long"},
                                          str(ts): {"date": ts, "duration": 600, "title": f"h{ts}"}},
                       "uuid-webtv-202": {"a": {"date": ts, "duration": 60, "title": "other"}}})


class Guide(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.box, self.t = FakeBox(), H + 120
        self.g = epg.Guide(self.tmp.name, self.box, now=lambda: self.t)

    def tearDown(self):
        self.tmp.cleanup()

    def test_page_reads_cache_only_and_queues_missing(self):
        progs, missing = self.g.window(H + 7200, 2, {201})
        self.assertEqual((progs, missing, self.box.calls), ({}, [H + 7200, H + 10800], []))   # no call from a page
        self.assertEqual(self.g.fetch_one(), epg.FETCH_INTERVAL_S)
        self.assertEqual(self.box.calls, [H + 7200])     # asked-for hours first, not the current one
        self.g.fetch_one()
        progs, missing = self.g.window(H + 7200, 2, {201})
        self.assertEqual(missing, [])
        self.assertEqual(list(progs), ["201"])            # only the playlist channels
        self.assertEqual([p["start"] for p in progs["201"]], [H + 7200, H + 10800])

    def test_merges_and_survives_restart(self):
        for _ in range(2):
            self.g.fetch_one()                           # H and H+1h, soonest first
        self.assertEqual(self.box.calls, [H, H + 3600])
        g2 = epg.Guide(self.tmp.name, FakeBox(), now=lambda: self.t)   # new process: read from disk
        progs, missing = g2.window(H, 2, {201})
        self.assertEqual(missing, [])
        self.assertEqual([p["start"] for p in progs["201"]], [H, H + 3000, H + 3600])   # deduplicated, sorted

    def test_refresh_and_horizon(self):
        for _ in range(epg.HORIZON_H):
            self.g.fetch_one()
        self.assertEqual(len(set(self.box.calls)), epg.HORIZON_H)
        self.assertEqual(self.g.fetch_one(), 60)         # all fresh: idle
        self.assertEqual(len(self.box.calls), epg.HORIZON_H)
        self.t += epg.REFRESH_SOON_S + 10                # the coming hours are refetched first
        self.assertEqual(self.g.next_slot(), H + 3 * 3600)

    def test_429_backs_off_and_retries_the_same_hour(self):
        self.box.status = 429
        self.assertEqual(self.g.fetch_one(), 60)
        self.assertEqual(self.g.fetch_one(), 120)
        self.box.status = None
        self.assertEqual(self.g.fetch_one(), epg.FETCH_INTERVAL_S)
        self.assertEqual(self.box.calls, [H, H, H])
        self.assertEqual(self.g.backoff, 0)

    def test_bad_answer_does_not_kill_the_loop(self):
        def bad(ts):
            return {"success": False}
        g = epg.Guide(self.tmp.name, bad, now=lambda: self.t)
        self.assertEqual(g.fetch_one(), 120)

    def test_old_hours_pruned(self):
        self.g.fetch_one()
        self.t += 2 * 86400
        self.g.fetch_one()
        self.assertNotIn(f"{H}.json", os.listdir(self.tmp.name))


if __name__ == "__main__":
    unittest.main()
