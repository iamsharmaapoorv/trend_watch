import os
import sys
from dataclasses import replace
from datetime import datetime, timedelta, timezone

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from candles import Candle  # noqa: E402
from config import Config  # noqa: E402

IST = timezone(timedelta(hours=5, minutes=30))


def ts(hm: str, day: str = "2026-11-18") -> int:
    return int(datetime.strptime(f"{day} {hm}", "%Y-%m-%d %H:%M").replace(tzinfo=IST).timestamp())


def five(start_hm: str, closes: list, first_open: float, day: str = "2026-11-18") -> list:
    """Back-to-back 5m candles: each opens at the previous close. high/low are 1 point outside the body."""
    out, t, o = [], ts(start_hm, day), first_open
    for c in closes:
        out.append(Candle(t, o, max(o, c) + 1, min(o, c) - 1, c))
        t, o = t + 300, c
    return out


def steady(start_hm: str, n: int, start: float, step: float, day: str = "2026-11-18") -> list:
    return five(start_hm, [start + step * (i + 1) for i in range(n)], start, day)


def cfg(**kw) -> Config:
    """Every test pins its own settings rather than relying on defaults."""
    base = Config(
        intervals=(15,),
        enabled=None,
        independent=False,
        min_streak={5: 6, 15: 4},
        min_move_pct={"NIFTY": {5: 0.25, 15: 0.30}, "BANKNIFTY": {5: 0.35, 15: 0.40}},
        hold_candles={5: None, 15: None},
    )
    return replace(base, **kw)


def run(candles, config, index="NIFTY", start="09:20", end="15:30", state=None, day="2026-11-18"):
    """Step the engine every 5 minutes like cron would. Returns (state, all alerts)."""
    import engine

    alerts, t, stop = [], ts(start, day), ts(end, day)
    state = state or {}
    while t <= stop:
        state, new = engine.step(state, {index: candles}, t, config)
        alerts += new
        t += 300
    return state, alerts


def of(alerts, kind):
    return [a for a in alerts if a["type"] == kind]
