import unittest

from tests.helpers import steady, ts
import candles as cd


class ParseTests(unittest.TestCase):
    def test_groww_parses_and_skips_null_rows(self):
        payload = {"candles": [[ts("09:20"), 2, 3, 1, 2.5, None], [ts("09:15"), 1, 2, 0.5, 2, None], [ts("09:25"), None, 1, 1, 1, None]]}
        out = cd.parse_groww(payload)
        self.assertEqual([c.ts for c in out], [ts("09:15"), ts("09:20")])
        self.assertEqual(out[0], cd.Candle(ts("09:15"), 1.0, 2.0, 0.5, 2.0))

    def test_yahoo_skips_nulls_and_off_grid_tick(self):
        t = ts("09:15")
        payload = {"chart": {"result": [{"timestamp": [t, t + 300, t + 600, t + 727],
                   "indicators": {"quote": [{"open": [1, None, 3, 9], "high": [2, 2, 4, 9], "low": [0, 1, 2, 9], "close": [1.5, 2, 3.5, 9]}]}}]}}
        out = cd.parse_yahoo(payload)
        self.assertEqual([c.ts for c in out], [t, t + 600])


class SessionTests(unittest.TestCase):
    def test_forming_candle_dropped(self):
        c5 = steady("09:15", 3, 100, 1)  # 09:15, 09:20, 09:25
        out = cd.session_completed(c5, ts("09:27"))  # 09:25 candle still forming
        self.assertEqual([c.ts for c in out], [ts("09:15"), ts("09:20")])

    def test_only_todays_session(self):
        c5 = steady("15:20", 2, 100, 1, day="2026-11-17") + steady("09:00", 4, 100, 1)  # incl. pre-open 09:00-09:10
        out = cd.session_completed(c5, ts("10:00"))
        self.assertEqual([c.ts for c in out], [ts("09:15")])


class AggregateTests(unittest.TestCase):
    def test_15m_anchored_at_0915_and_only_complete_buckets(self):
        c5 = steady("09:15", 5, 100, 1)  # through 09:40 close
        out = cd.aggregate(c5, 15)
        self.assertEqual(len(out), 1)  # 09:30 bucket is incomplete
        self.assertEqual(out[0], cd.Candle(ts("09:15"), 100, 104, 99, 103))
        out = cd.aggregate(steady("09:15", 6, 100, 1), 15)
        self.assertEqual([c.ts for c in out], [ts("09:15"), ts("09:30")])


class FallbackTests(unittest.TestCase):
    def test_falls_back_when_primary_fails(self):
        def bad(symbol, now):
            raise RuntimeError("down")

        good = lambda symbol, now: steady("09:15", 1, 100, 1)
        out, source = cd.fetch_5m("NIFTY", ts("10:00"), fetchers=(("a", bad), ("b", good)))
        self.assertEqual(source, "b")
        self.assertEqual(len(out), 1)

    def test_empty_primary_falls_back_and_all_failing_raises(self):
        empty = lambda symbol, now: []
        good = lambda symbol, now: steady("09:15", 1, 100, 1)
        self.assertEqual(cd.fetch_5m("NIFTY", 0, fetchers=(("a", empty), ("b", good)))[1], "b")
        with self.assertRaises(cd.DataError):
            cd.fetch_5m("NIFTY", 0, fetchers=(("a", empty),))


if __name__ == "__main__":
    unittest.main()
