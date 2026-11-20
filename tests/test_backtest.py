import unittest

from tests.helpers import steady, ts
import backtest


class CostTests(unittest.TestCase):
    def test_zero_move_trade_loses_exactly_the_costs(self):
        trade = {"index": "NIFTY", "points": 0.0, "entry_ts": ts("10:00"), "exit_ts": ts("10:30")}
        self.assertAlmostEqual(backtest.net_points(trade), -(4.0 + 2.0 * 0.5))

    def test_costs_charged_once_per_trade(self):
        trade = {"index": "BANKNIFTY", "points": 50.0, "entry_ts": ts("10:00"), "exit_ts": ts("12:00")}
        self.assertAlmostEqual(backtest.net_points(trade), 50 - 10 - 5 * 2)


class SimulationTests(unittest.TestCase):
    def test_run_day_uses_engine_rules(self):
        cfg = backtest.make_cfg("NIFTY", 5, 3, 0.1, 2)  # 3 rising 5m candles, exit after 2 more
        trades = backtest.run_day("NIFTY", steady("09:15", 20, 25000, 10), cfg)
        self.assertEqual((trades[0]["reason"], trades[0]["points"]), ("hold", 20))
        self.assertEqual(len(trades), 1)  # one streak, one trade

    def test_forced_baseline_entry(self):
        cfg = backtest.make_cfg("NIFTY", 5, 3, 0.1, 2)
        t = backtest.run_forced("NIFTY", steady("09:15", 20, 25000, 10), cfg, 10 * 60, 1, 5)
        self.assertEqual((t["entry_ts"], t["exit_ts"], t["points"]), (ts("10:00"), ts("10:10"), 20))
        falling = backtest.run_forced("NIFTY", steady("09:15", 20, 25000, 10), cfg, 10 * 60, -1, 5)
        self.assertEqual(falling["reason"], "stop")  # a short into a rising market is stopped out
        self.assertLess(falling["points"], 0)


if __name__ == "__main__":
    unittest.main()
