"""Pure signal logic. No I/O.

Definitions
- Up candle: close > open. Down: close < open. Flat: |body| < flat_pct % of open.
- Streak: consecutive same-direction candles ending at the latest candle; flats neither count nor break.
- Net move: first streak candle's open -> last streak candle's close, % of that open.
- Stop: low (rising) / high (falling) of the latest streak candle. Only ever tightens.
"""
from dataclasses import dataclass

from candles import Candle


@dataclass(frozen=True)
class Streak:
    direction: int  # +1 rising, -1 falling
    length: int
    start_ts: int
    start_open: float
    last_ts: int
    last_close: float
    stop: float
    net_pct: float  # signed


def candle_direction(c: Candle, flat_pct: float) -> int:
    body = c.close - c.open
    if abs(body) < c.open * flat_pct / 100:
        return 0
    return 1 if body > 0 else -1


def current_streak(candles: list[Candle], flat_pct: float) -> Streak | None:
    direction, members = 0, []
    for c in reversed(candles):
        d = candle_direction(c, flat_pct)
        if d == 0:
            continue
        if direction == 0:
            direction = d
        if d != direction:
            break
        members.append(c)
    if not members:
        return None
    last, first = members[0], members[-1]
    return Streak(
        direction=direction,
        length=len(members),
        start_ts=first.ts,
        start_open=first.open,
        last_ts=last.ts,
        last_close=last.close,
        stop=last.low if direction > 0 else last.high,
        net_pct=(last.close - first.open) / first.open * 100,
    )


def qualifies(streak: Streak, min_streak: int, min_move_pct: float) -> bool:
    return streak.length >= min_streak and abs(streak.net_pct) >= min_move_pct


def tighten_stop(stop: float, candidate: float, direction: int) -> float:
    return max(stop, candidate) if direction > 0 else min(stop, candidate)


def update_stop(stop: float, c: Candle, direction: int, flat_pct: float) -> float:
    """Trail the stop to a new same-direction candle's low/high. Never loosens."""
    if candle_direction(c, flat_pct) != direction:
        return stop
    return tighten_stop(stop, c.low if direction > 0 else c.high, direction)


def stop_breached(stop: float, c: Candle, direction: int) -> bool:
    """A breach is a close beyond the stop; a wick through does not count."""
    return (c.close - stop) * direction < 0


def suggest_hold(index: str, interval: int, cfg, hold_stats: dict | None):
    """Hold horizon in candles for this pairing (None = no time limit). The config is the single
    source of truth; hold_stats only supplies the history line shown in alerts."""
    return cfg.hold_for(index, interval)
