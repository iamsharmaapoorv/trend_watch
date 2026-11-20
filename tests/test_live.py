import json
import tempfile
import unittest
from pathlib import Path

from tests.helpers import cfg, five, steady, ts
import journal
import trend_watch as tw
from state import StateStore

C5 = dict(intervals=(5,), min_streak={5: 3, 15: 4}, min_move_pct={"NIFTY": {5: 0.1, 15: 9}, "BANKNIFTY": {5: 0.1, 15: 9}})


class Harness(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.journal = Path(self.dir.name) / "journal.csv"
        self.sent, self.fail = [], False
        self.candles = {}

    def tearDown(self):
        self.dir.cleanup()

    def send(self, text):
        if self.fail:
            return False
        self.sent.append(text)
        return True

    def source(self, index, now):
        value = self.candles.get(index, [])
        if isinstance(value, Exception):
            raise value
        return value

    def run_at(self, hm, state, config, day="2026-11-18"):
        return tw.run_once(ts(hm, day), config, state, self.send, sources=(("fake", self.source),),
                           journal_path=self.journal, retry_wait=0)

    def rows(self):
        import csv
        if not self.journal.exists():
            return []
        with open(self.journal) as fh:
            return list(csv.DictReader(fh))


class LiveRunTests(Harness):
    def test_entry_then_exit_are_sent_and_journaled(self):
        self.candles["NIFTY"] = five("09:15", [25010, 25020, 25030], 25000)
        state = self.run_at("09:30", {}, cfg(**C5))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("buy CE", self.sent[0])
        self.assertIn("PAPER", self.sent[0])
        self.candles["NIFTY"] = five("09:15", [25010, 25020, 25030, 25000], 25000)
        state = self.run_at("09:35", state, cfg(**C5))
        self.assertIn("EXIT NIFTY", self.sent[1])
        self.assertEqual([r["event"] for r in self.rows()], ["ENTRY", "EXIT"])
        self.assertEqual(float(self.rows()[1]["points"]), -30)
        self.assertEqual(state["trades"], {})

    def test_falling_alert_says_buy_pe_and_nothing_mentions_selling(self):
        self.candles["NIFTY"] = five("09:15", [24990, 24980, 24970], 25000)
        state = self.run_at("09:30", {}, cfg(**C5))
        self.candles["NIFTY"] = five("09:15", [24990, 24980, 24970, 25000], 25000)
        self.run_at("09:35", state, cfg(**C5))
        self.assertIn("buy PE", self.sent[0])
        self.assertTrue(all("sell" not in t.lower() for t in self.sent))
        self.assertEqual(len(self.sent), 2)

    def test_undelivered_entry_is_dropped_not_retried(self):
        self.candles["NIFTY"] = five("09:15", [25010, 25020, 25030], 25000)
        self.fail = True
        state = self.run_at("09:30", {}, cfg(**C5))
        self.assertEqual(state["trades"], {})  # no trade the user never heard about
        self.assertEqual(state["outbox"], [])
        self.assertEqual(self.rows(), [])
        self.fail = False
        self.candles["NIFTY"] = five("09:15", [25010, 25020, 25030, 25040], 25000)
        self.run_at("09:35", state, cfg(**C5))
        self.assertEqual(self.sent, [])  # same streak is not re-alerted at a later, worse price

    def test_stale_entry_is_dropped(self):
        # First run of the day happens long after the streak first qualified.
        self.candles["NIFTY"] = steady("09:15", 8, 25000, 10)
        state = self.run_at("09:55", {}, cfg(**C5))
        self.assertEqual((self.sent, state["trades"]), ([], {}))

    def test_undelivered_exit_is_retried_until_sent(self):
        self.candles["NIFTY"] = five("09:15", [25010, 25020, 25030], 25000)
        state = self.run_at("09:30", {}, cfg(**C5))
        self.candles["NIFTY"] = five("09:15", [25010, 25020, 25030, 25000], 25000)
        self.fail = True
        state = self.run_at("09:35", state, cfg(**C5))
        self.assertEqual(len(state["outbox"]), 1)
        self.assertEqual(len(self.rows()), 2)  # journaled even though Telegram failed
        self.fail = False
        self.candles["NIFTY"] = five("09:15", [25010, 25020, 25030, 25000, 25001], 25000)
        state = self.run_at("09:40", state, cfg(**C5))
        self.assertEqual(state["outbox"], [])
        self.assertIn("EXIT NIFTY", self.sent[-1])
        self.assertEqual(len(self.rows()), 2)  # not journaled twice

    def test_data_down_warns_once_per_day(self):
        self.candles = {"NIFTY": RuntimeError("down"), "BANKNIFTY": RuntimeError("down")}
        state = self.run_at("10:00", {}, cfg(**C5))
        state = self.run_at("10:05", state, cfg(**C5))
        self.assertEqual(len(self.sent), 1)
        self.assertIn("no fresh data for NIFTY, BANKNIFTY", self.sent[0])
        state = self.run_at("10:00", state, cfg(**C5), day="2026-11-19")
        self.assertEqual(len(self.sent), 2)

    def test_stale_source_counts_as_down(self):
        self.candles = {"NIFTY": steady("09:15", 3, 25000, 1), "BANKNIFTY": steady("09:15", 3, 25000, 1)}
        self.run_at("10:00", {}, cfg(**C5))  # latest candle closed 30 minutes ago
        self.assertIn("no fresh data", self.sent[0])

    def test_holiday_sends_nothing(self):
        yesterday = steady("09:15", 75, 25000, 1, day="2026-11-17")
        self.candles = {"NIFTY": yesterday, "BANKNIFTY": yesterday}
        state = self.run_at("10:00", {}, cfg(**C5))
        self.assertEqual(self.sent, [])
        self.assertNotIn("data_down_day", state)

    def test_outside_market_hours_does_nothing(self):
        self.candles = {"NIFTY": RuntimeError("down")}
        self.assertEqual(self.run_at("08:45", {"x": 1}, cfg(**C5)), {"x": 1})
        self.assertEqual(self.sent, [])

    def test_time_up_message_and_rule_points_in_journal(self):
        c = cfg(**C5, hold_candles={5: 2, 15: None})
        closes = [25010, 25020, 25030, 25040, 25050, 25060, 25040]  # hold reached at 09:40 (+20), stop out 09:50
        state = {}
        for i, hm in enumerate(["09:30", "09:35", "09:40", "09:45", "09:50"]):
            self.candles["NIFTY"] = five("09:15", closes[: i + 3], 25000)
            state = self.run_at(hm, state, c)
        self.assertEqual(sum("suggested hold reached" in t for t in self.sent), 1)
        exit_row = self.rows()[-1]
        self.assertEqual((float(exit_row["points"]), float(exit_row["rule_points"])), (10, 20))
        s = journal.summary(self.journal)["NIFTY 5m"]
        self.assertEqual(s["trades"], 1)
        self.assertAlmostEqual(s["total_net"], 20 - float(exit_row["rule_cost"]))
        self.assertAlmostEqual(s["trailed_total_net"], 10 - float(exit_row["est_cost"]))


class StateTests(unittest.TestCase):
    def test_roundtrip_is_atomic_and_corrupt_file_loads_empty(self):
        with tempfile.TemporaryDirectory() as d:
            store = StateStore(Path(d) / "s.json")
            self.assertEqual(store.load(), {})
            store.save({"a": 1})
            self.assertEqual(store.load(), {"a": 1})
            self.assertEqual([p.name for p in Path(d).iterdir()], ["s.json"])  # no temp file left behind
            (Path(d) / "s.json").write_text('{"a": 1')
            self.assertEqual(store.load(), {})


class MessageTests(unittest.TestCase):
    def test_entry_message_shows_hold_stop_and_history(self):
        import engine
        c = cfg(**C5, hold_candles={5: 3, 15: None})
        _, alerts = engine.step({}, {"NIFTY": five("09:15", [25010, 25020, 25030], 25000)}, ts("09:30"), c)
        stats = {"NIFTY:5": {"hold_candles": 3, "days": 44, "n": 24, "win_pct": 46.0, "base_win_pct": 38.0, "net": 6.9}}
        text = tw.build_message(alerts[0], c, stats)
        self.assertIn("NIFTY rising — 3 × 5m candles, +0.12% (25,000 → 25,030)", text)
        self.assertIn("Hold ≈ 15 min (to ~09:45 IST)", text)
        self.assertIn("Stop 25,019, trailing to the low of each new rising 5m candle", text)
        self.assertIn("24 similar signals, 46% profitable vs 38% baseline", text)


if __name__ == "__main__":
    unittest.main()
