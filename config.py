"""Settings. Defaults are starting values; the backtest confirms or replaces them."""
import os
from dataclasses import dataclass, field

INDICES = ("NIFTY", "BANKNIFTY")

# Rough cost stand-ins, in index points, used by the backtest and the journal summary.
COST_ROUND_TRIP = {"NIFTY": 4.0, "BANKNIFTY": 10.0}  # spread, fees, late entry
COST_PER_HOUR = {"NIFTY": 2.0, "BANKNIFTY": 5.0}  # premium decay on a bought option


@dataclass(frozen=True)
class Config:
    """min_streak, min_move_pct and hold_candles are keyed {index: {interval: value}}.
    A flat {interval: value} dict is also accepted and applies to every index."""

    # All four pairings run side by side in paper mode so the journal can compare them.
    # Each pairing uses the setting that scored best on the backtest's fit days (2026-11-20).
    # On the hold-out days BankNifty 5m and Nifty 15m passed; Nifty 5m and BankNifty 15m did not.
    intervals: tuple = (5, 15)
    # Which timeframes may open trades, per index. None = every interval in `intervals`.
    enabled: dict | None = field(default_factory=lambda: {"NIFTY": (5, 15), "BANKNIFTY": (5, 15)})
    min_streak: dict = field(default_factory=lambda: {"NIFTY": {5: 5, 15: 4}, "BANKNIFTY": {5: 5, 15: 5}})
    min_move_pct: dict = field(
        default_factory=lambda: {"NIFTY": {5: 0.30, 15: 0.35}, "BANKNIFTY": {5: 0.39, 15: 0.455}}
    )
    # Suggested hold, in candles of the signal's own size. None = no time limit (trail only).
    hold_candles: dict = field(default_factory=lambda: {"NIFTY": {5: 6, 15: None}, "BANKNIFTY": {5: 3, 15: 4}})
    # True: each index+timeframe pairing holds its own trade, unaffected by the others.
    # False: one trade per index; the other timeframe only confirms it or reverses it.
    independent: bool = True
    flat_pct: float = 0.01  # body smaller than this % of open is a flat candle
    no_entry_after: int = 14 * 60 + 30  # IST minute of day
    hard_exit: int = 15 * 60 + 15
    allow_entries: bool = True
    # False (live): at hold_until send TIME'S UP and keep trailing. True (backtest): exit there.
    exit_at_hold: bool = False
    # Optional filters, off by default; the backtest tests each one alone.
    require_day_direction: bool = False  # signal must agree with price vs the 09:15 open
    entry_window: tuple | None = None  # (start, end) IST minutes of day
    min_first_hour_range_pct: float = 0.0  # skip quiet days; entries only after 10:15
    paper: bool = True

    @staticmethod
    def _per(table: dict, index: str, interval: int):
        return table[index][interval] if index in table else table[interval]

    def streak_for(self, index, interval) -> int:
        return self._per(self.min_streak, index, interval)

    def move_for(self, index, interval) -> float:
        return self._per(self.min_move_pct, index, interval)

    def hold_for(self, index, interval):
        return self._per(self.hold_candles, index, interval)


def _num(name: str, default, cast):
    raw = os.getenv(name)
    if raw is None or raw.strip() == "":
        return default
    return cast(raw)


def _hold(raw: str):
    return None if raw.strip().lower() in ("0", "none", "trail") else int(raw)


def _intervals(raw: str) -> tuple:
    return tuple(int(x) for x in raw.replace(" ", "").split(",") if x)


def load_config() -> Config:
    base = Config()

    def table(env: str, source: dict, cast) -> dict:
        return {index: {iv: _num(f"TREND_WATCH_{env}_{index}_{iv}", source[index][iv], cast) for iv in base.intervals}
                for index in INDICES}

    return Config(
        enabled={index: _num(f"TREND_WATCH_ENABLED_{index}", base.enabled[index], _intervals) for index in INDICES},
        min_streak=table("MIN_STREAK", base.min_streak, int),
        min_move_pct=table("MIN_MOVE", base.min_move_pct, float),
        hold_candles=table("HOLD_CANDLES", base.hold_candles, _hold),
        paper=_num("TREND_WATCH_PAPER", 1, int) != 0,
    )
