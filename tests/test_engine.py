import unittest
from dataclasses import replace

from tests.helpers import cfg, five, of, run, steady, ts
from candles import Candle
import engine
import signals


class EntryTests(unittest.TestCase):
    """Nifty 15m, min 4 candles / 0.30%. 25000 base: +22 per 5m = +66 per 15m candle."""

    def test_three_candles_no_signal(self):
        _, alerts = run(steady("09:15", 9, 25000, 8), cfg(), end="10:00")
        self.assertEqual(of(alerts, "ENTRY"), [])

    def test_four_candles_with_enough_move_enters(self):
        # 12 x 5m, +7.5 each = +90 = 0.36%
        _, alerts = run(steady("09:15", 12, 25000, 7.5), cfg(), end="10:15")
        entries = of(alerts, "ENTRY")
        self.assertEqual(len(entries), 1)
        trade = entries[0]["trade"]
        self.assertEqual((trade["direction"], trade["interval"], trade["streak_len"]), (1, 15, 4))
        self.assertEqual(trade["entry_ts"], ts("10:15"))
        self.assertAlmostEqual(trade["net_pct"], 0.36, places=2)

    def test_four_candles_with_small_move_no_signal(self):
        _, alerts = run(steady("09:15", 12, 25000, 2), cfg(), end="10:15")  # +0.096%
        self.assertEqual(of(alerts, "ENTRY"), [])

    def test_banknifty_needs_larger_move(self):
        _, alerts = run(steady("09:15", 12, 25000, 7.5), cfg(), index="BANKNIFTY", end="10:15")  # 0.36% < 0.40%
        self.assertEqual(of(alerts, "ENTRY"), [])

    def test_5m_needs_six_candles(self):
        c = cfg(intervals=(5,))
        _, alerts = run(steady("09:15", 5, 25000, 13), c, end="09:40")
        self.assertEqual(of(alerts, "ENTRY"), [])
        _, alerts = run(steady("09:15", 6, 25000, 13), c, end="09:45")  # +0.312%
        self.assertEqual(len(of(alerts, "ENTRY")), 1)

    def test_falling_mirror(self):
        _, alerts = run(steady("09:15", 12, 25000, -7.5), cfg(), end="10:15")
        self.assertEqual(of(alerts, "ENTRY")[0]["trade"]["direction"], -1)

    def test_flat_candle_neither_counts_nor_breaks(self):
        c = cfg(intervals=(5,), min_streak={5: 4, 15: 4})
        closes = [25020, 25040, 25041, 25060, 25080]  # third candle body = 1pt = 0.004% -> flat
        _, alerts = run(five("09:15", closes, 25000), c, end="09:35")
        self.assertEqual(of(alerts, "ENTRY"), [])  # 4 candles seen, but the flat one does not count
        _, alerts = run(five("09:15", closes, 25000), c, end="09:40")
        self.assertEqual(of(alerts, "ENTRY")[0]["trade"]["streak_len"], 4)

    def test_opposite_candle_resets_and_new_streak_gets_new_key(self):
        c = cfg(intervals=(5,), min_streak={5: 3, 15: 4}, min_move_pct={"NIFTY": {5: 0.1, 15: 9}})
        closes = [25010, 25020, 24990, 25005, 25020, 25035]  # 2 up, 1 down, 3 up
        _, alerts = run(five("09:15", closes, 25000), c, end="09:45")
        entries = of(alerts, "ENTRY")
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["key"], f"NIFTY:5:{ts('09:30')}")

    def test_same_streak_alerts_once(self):
        _, alerts = run(steady("09:15", 30, 25000, 7.5), cfg(), end="11:45")
        self.assertEqual(len(of(alerts, "ENTRY")), 1)

    def test_no_entry_after_1430(self):
        _, alerts = run(steady("13:45", 12, 25000, 7.5), cfg(), start="13:50", end="14:45")  # qualifies at 14:45
        self.assertEqual(of(alerts, "ENTRY"), [])
        _, alerts = run(steady("13:25", 12, 25000, 7.5), cfg(intervals=(5,)), start="13:30", end="14:25")
        self.assertEqual(len(of(alerts, "ENTRY")), 1)

    def test_streak_does_not_span_yesterday(self):
        y = steady("14:30", 9, 25000, 7.5, day="2026-11-17")  # 3 rising 15m candles yesterday
        t = steady("09:15", 3, 25070, 7.5)  # 1 rising 15m candle today
        _, alerts = run(y + t, cfg(), end="09:30")
        self.assertEqual(of(alerts, "ENTRY"), [])


class CrossTimeframeTests(unittest.TestCase):
    def test_second_timeframe_confirms_without_new_entry(self):
        c = cfg(intervals=(5, 15))
        state, alerts = run(steady("09:15", 12, 25000, 13), c, end="10:15")
        self.assertEqual([a["interval"] for a in of(alerts, "ENTRY")], [5])  # 5m fires at 09:45
        self.assertEqual([a["interval"] for a in of(alerts, "CONFIRM")], [15])  # 15m at 10:15, log only
        trade = state["trades"]["NIFTY"]
        self.assertEqual(trade["interval"], 5)  # the opening timeframe's stop still governs
        self.assertEqual(trade["confirmed_by"], [15])
        self.assertEqual(trade["stop"], 25000 + 13 * 11 - 1)  # low of the latest 5m candle

    def _choppy_rise(self):
        # each 15m candle: -4, +40, -4 on 5m. 15m rises +32; 5m never has 2 ups in a row,
        # and its 2-down runs move only 0.032%
        closes, p = [], 25000
        for _ in range(4):
            for d in (-4, 40, -4):
                p += d
                closes.append(p)
        return closes  # ends 25128 at 10:15

    def test_opposite_signal_exits_then_enters(self):
        c = cfg(intervals=(5, 15), min_streak={5: 2, 15: 4}, min_move_pct={"NIFTY": {5: 0.04, 15: 0.30}})
        closes = self._choppy_rise() + [25116]  # 2 falling 5m candles, 0.064%; 15m stop is 25091
        state, alerts = run(five("09:15", closes, 25000), c, end="10:25")
        kinds = [(a["type"], a.get("reason")) for a in alerts if a["type"] != "CONFIRM"]
        self.assertEqual(kinds, [("ENTRY", None), ("EXIT", "opposite_signal"), ("ENTRY", None)])
        self.assertEqual(state["trades"]["NIFTY"]["direction"], -1)

    def test_15m_trade_not_stopped_by_5m_close(self):
        c = cfg()  # 15m only
        closes = self._choppy_rise()  # 15m entry at 10:15; stop = low of last 15m candle = 25091
        dip = closes + [25080, 25100, 25110]  # 5m close below stop at 10:20, 15m candle closes above it
        state, alerts = run(five("09:15", dip, 25000), c, end="10:30")
        self.assertEqual(of(alerts, "EXIT"), [])
        self.assertIn("NIFTY", state["trades"])


class ExitTests(unittest.TestCase):
    C5 = dict(intervals=(5,), min_streak={5: 3, 15: 4}, min_move_pct={"NIFTY": {5: 0.1, 15: 9}})

    def _entered(self):
        return five("09:15", [25010, 25020, 25030], 25000)  # entry 09:30 at 25030, stop 25019

    def test_close_beyond_stop_exits(self):
        candles = self._entered() + [Candle(ts("09:30"), 25030, 25031, 25010, 25015)]
        _, alerts = run(candles, cfg(**self.C5), end="09:35")
        ex = of(alerts, "EXIT")[0]
        self.assertEqual((ex["reason"], ex["exit_price"], ex["points"]), ("stop", 25015, -15))

    def test_wick_through_stop_does_not_exit(self):
        candles = self._entered() + [Candle(ts("09:30"), 25030, 25031, 25000, 25025)]
        state, alerts = run(candles, cfg(**self.C5), end="09:35")
        self.assertEqual(of(alerts, "EXIT"), [])
        self.assertEqual(state["trades"]["NIFTY"]["stop"], 25019)  # down candle: stop unchanged

    def test_stop_trails_and_only_tightens(self):
        candles = self._entered() + [
            Candle(ts("09:30"), 25030, 25041, 25029, 25040),  # up: stop -> 25029
            Candle(ts("09:35"), 25040, 25051, 25025, 25050),  # up with deep wick: stop stays 25029
            Candle(ts("09:40"), 25050, 25052, 25020, 25050.5),  # flat: stop stays
        ]
        state, _ = run(candles[:4], cfg(**self.C5), end="09:35")
        self.assertEqual(state["trades"]["NIFTY"]["stop"], 25029)
        state, alerts = run(candles, cfg(**self.C5), end="09:45")
        self.assertEqual(state["trades"]["NIFTY"]["stop"], 25029)
        self.assertEqual(of(alerts, "EXIT"), [])

    def test_time_up_once_and_trade_stays_open(self):
        c = cfg(**self.C5, hold_candles={5: 2, 15: None})
        state, alerts = run(steady("09:15", 10, 25000, 10), c, end="10:05")
        self.assertEqual(len(of(alerts, "TIME_UP")), 1)
        self.assertEqual(of(alerts, "EXIT"), [])
        self.assertTrue(state["trades"]["NIFTY"]["time_up"])

    def test_exit_at_hold_for_backtest(self):
        c = cfg(**self.C5, hold_candles={5: 2, 15: None}, exit_at_hold=True)
        _, alerts = run(steady("09:15", 10, 25000, 10), c, end="10:05")
        ex = of(alerts, "EXIT")[0]
        self.assertEqual((ex["reason"], ex["exit_ts"], ex["points"]), ("hold", ts("09:40"), 20))

    def test_hard_exit_at_1515(self):
        _, alerts = run(steady("13:00", 30, 25000, 10), cfg(**self.C5), start="13:05", end="15:30")
        ex = of(alerts, "EXIT")
        self.assertEqual(len(ex), 1)
        self.assertEqual((ex[0]["reason"], ex[0]["exit_ts"]), ("session_end", ts("15:15")))

    def test_hold_never_extends_past_1515(self):
        c = cfg(**self.C5, hold_candles={5: 500, 15: None})
        state, _ = run(steady("09:15", 3, 25000, 10), c, end="09:30")
        self.assertEqual(state["trades"]["NIFTY"]["hold_until"], ts("15:15"))

    def test_new_session_clears_yesterdays_trade(self):
        state, _ = run(steady("09:15", 3, 25000, 10), cfg(**self.C5), end="09:30")
        self.assertIn("NIFTY", state["trades"])
        state, alerts = engine.step(state, {"NIFTY": []}, ts("09:16", day="2026-11-19"), cfg(**self.C5))
        self.assertEqual((state["trades"], state["alerted"], alerts), ({}, [], []))

    def test_no_data_no_crash(self):
        state, alerts = engine.step({}, {"NIFTY": [], "BANKNIFTY": []}, ts("11:00"), cfg())
        self.assertEqual(alerts, [])


class FilterAndHoldTests(unittest.TestCase):
    def test_day_direction_filter(self):
        c = cfg(intervals=(5,), min_streak={5: 3, 15: 4}, min_move_pct={"NIFTY": {5: 0.1, 15: 9}}, require_day_direction=True)
        down_then_up = five("09:15", [24900, 24800, 24700, 24730, 24760, 24790], 25000)  # rising streak, day still down
        _, alerts = run(down_then_up, c, end="09:45")
        self.assertEqual([a["trade"]["direction"] for a in of(alerts, "ENTRY")], [-1])

    def test_entry_window_filter(self):
        c = cfg(intervals=(5,), entry_window=(13 * 60 + 30, 14 * 60 + 30))
        _, alerts = run(steady("09:15", 8, 25000, 13), c, end="09:55")
        self.assertEqual(of(alerts, "ENTRY"), [])

    def test_suggest_hold_comes_from_config_per_pairing(self):
        c = cfg(hold_candles={"NIFTY": {5: 6, 15: None}, "BANKNIFTY": {5: 3, 15: 4}})
        self.assertEqual(signals.suggest_hold("NIFTY", 5, c, None), 6)
        self.assertIsNone(signals.suggest_hold("NIFTY", 15, c, None))
        self.assertEqual(signals.suggest_hold("BANKNIFTY", 15, c, {"BANKNIFTY:15": {"hold_candles": 9}}), 4)


class IndependentPairingTests(unittest.TestCase):
    """Paper mode: each index+timeframe pairing holds its own trade."""

    C = dict(intervals=(5, 15), independent=True,
             min_streak={"NIFTY": {5: 6, 15: 4}}, min_move_pct={"NIFTY": {5: 0.25, 15: 0.30}})

    def test_both_timeframes_open_their_own_trade(self):
        state, alerts = run(steady("09:15", 12, 25000, 13), cfg(**self.C), end="10:15")
        self.assertEqual([a["interval"] for a in of(alerts, "ENTRY")], [5, 15])
        self.assertEqual(of(alerts, "CONFIRM"), [])
        self.assertEqual(sorted(state["trades"]), ["NIFTY:15", "NIFTY:5"])

    def test_one_pairing_exiting_leaves_the_other_open(self):
        # after both are open, one 5m candle closes below the 5m stop but above the 15m stop
        candles = steady("09:15", 12, 25000, 13) + five("10:15", [25140, 25150, 25160], 25156)
        state, alerts = run(candles, cfg(**self.C), end="10:30")
        exits = of(alerts, "EXIT")
        self.assertEqual([(e["interval"], e["reason"]) for e in exits], [(5, "stop")])
        self.assertEqual(list(state["trades"]), ["NIFTY:15"])

    def test_live_defaults_cover_all_four_pairings(self):
        from config import Config
        c = Config()
        self.assertTrue(c.independent)
        self.assertEqual(c.enabled, {"NIFTY": (5, 15), "BANKNIFTY": (5, 15)})
        self.assertEqual([c.streak_for("BANKNIFTY", 15), c.move_for("BANKNIFTY", 15), c.hold_for("BANKNIFTY", 15)], [5, 0.455, 4])
        self.assertEqual([c.streak_for("NIFTY", 5), c.move_for("NIFTY", 5), c.hold_for("NIFTY", 5)], [5, 0.30, 6])


if __name__ == "__main__":
    unittest.main()
