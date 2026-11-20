"""One run's decision logic, with no I/O. Live runs, --replay and the backtest all call step()."""
import copy

import signals
from candles import FIVE_MIN, Candle, aggregate, ist_day, ist_mod, ist_str, ist_ts, session_completed

FIRST_HOUR_END = 10 * 60 + 15


def _frames(c5: list[Candle], intervals) -> dict:
    return {iv: aggregate(c5, iv) for iv in set(intervals) | {5}}


def _hold_until(now_ts: int, interval: int, hold, cfg) -> int:
    hard = ist_ts(ist_day(now_ts), cfg.hard_exit)
    if hold is None:
        return hard
    return min(hard, now_ts + hold * interval * 60)


def open_trade(index, interval, direction, c5, frame, now_ts, cfg, ctx, key=None, streak=None) -> dict:
    last = frame[-1] if frame else c5[-1]
    stop = streak.stop if streak else (last.low if direction > 0 else last.high)
    hold = signals.suggest_hold(index, interval, cfg, (ctx or {}).get("hold_stats"))
    return {
        "index": index,
        "interval": interval,
        "direction": direction,
        "key": key,
        "entry_ts": now_ts,
        "entry_price": c5[-1].close,
        "stop": stop,
        "hold_candles": hold,
        "hold_until": _hold_until(now_ts, interval, hold, cfg),
        "last_seen_ts": last.ts,
        "streak_len": streak.length if streak else 0,
        "net_pct": streak.net_pct if streak else 0.0,
        "streak_start_price": streak.start_open if streak else None,
        "time_up": False,
        "time_up_ts": None,
        "time_up_price": None,
        "confirmed_by": [],
    }


def _exit(trade, price, reason, now_ts) -> dict:
    return {
        "type": "EXIT",
        "reason": reason,
        "index": trade["index"],
        "interval": trade["interval"],
        "direction": trade["direction"],
        "entry_ts": trade["entry_ts"],
        "entry_price": trade["entry_price"],
        "exit_ts": now_ts,
        "exit_price": price,
        "points": (price - trade["entry_price"]) * trade["direction"],
        # Result at the suggested hold time, if that was reached before this exit (the tested rule).
        "hold_ts": trade.get("time_up_ts"),
        "hold_points": None if trade.get("time_up_price") is None
        else (trade["time_up_price"] - trade["entry_price"]) * trade["direction"],
        "key": trade["key"],
    }


def _manage(trade, frames, c5, now_ts, cfg):
    """Returns (exit_alert_or_None, other_alerts). Mutates trade (stop, last_seen_ts, time_up)."""
    d = trade["direction"]
    for c in frames[trade["interval"]]:
        if c.ts <= trade["last_seen_ts"]:
            continue
        trade["last_seen_ts"] = c.ts
        if signals.stop_breached(trade["stop"], c, d):
            return _exit(trade, c.close, "stop", now_ts), []
        trade["stop"] = signals.update_stop(trade["stop"], c, d, cfg.flat_pct)
    if ist_mod(now_ts) >= cfg.hard_exit:
        return _exit(trade, c5[-1].close, "session_end", now_ts), []
    if now_ts >= trade["hold_until"] and not trade["time_up"]:
        if cfg.exit_at_hold:
            return _exit(trade, c5[-1].close, "hold", now_ts), []
        trade["time_up"] = True
        trade["time_up_ts"], trade["time_up_price"] = now_ts, c5[-1].close
        return None, [{"type": "TIME_UP", "index": trade["index"], "price": c5[-1].close, "trade": dict(trade)}]
    return None, []


def _filters_pass(direction, c5, mod, cfg) -> bool:
    if cfg.entry_window and not (cfg.entry_window[0] <= mod < cfg.entry_window[1]):
        return False
    if cfg.require_day_direction and (c5[-1].close - c5[0].open) * direction <= 0:
        return False
    if cfg.min_first_hour_range_pct > 0:
        if mod < FIRST_HOUR_END:
            return False
        first = [c for c in c5 if ist_mod(c.ts) < FIRST_HOUR_END]
        rng = (max(c.high for c in first) - min(c.low for c in first)) / first[0].open * 100
        if rng < cfg.min_first_hour_range_pct:
            return False
    return True


def step(state: dict, candles_by_index: dict, now_ts: int, cfg, ctx: dict | None = None):
    """Evaluate one run. Returns (new_state, alerts). Alerts are dicts with a "type":
    ENTRY, EXIT, TIME_UP or CONFIRM (CONFIRM is for the log only)."""
    state = copy.deepcopy(state) if state else {}
    ctx = ctx or {}
    today = ist_str(now_ts, "%Y-%m-%d")
    if state.get("session") != today:  # new session: yesterday's trades and keys are void
        state["session"] = today
        state["alerted"] = []
        state["trades"] = {}
    alerts = []
    mod = ist_mod(now_ts)
    vol_scale = ctx.get("vol_scale", 1.0)

    for index, candles in candles_by_index.items():
        c5 = session_completed(candles, now_ts)
        if not c5:
            continue
        ivs = cfg.intervals if cfg.enabled is None else cfg.enabled.get(index, ())
        trades = state["trades"]
        open_slots = [slot for slot, t in trades.items() if t["index"] == index]
        frames = _frames(c5, tuple(ivs) + tuple(trades[slot]["interval"] for slot in open_slots))
        for slot in open_slots:
            exit_alert, extra = _manage(trades[slot], frames, c5, now_ts, cfg)
            alerts.extend(extra)
            if exit_alert:
                alerts.append(exit_alert)
                del trades[slot]

        if not cfg.allow_entries or mod >= cfg.no_entry_after:
            continue
        for iv in ivs:
            frame = frames[iv]
            streak = signals.current_streak(frame, cfg.flat_pct) if frame else None
            if not streak:
                continue
            key = f"{index}:{iv}:{streak.start_ts}"
            if key in state["alerted"]:
                continue
            # A slot holds one trade: per pairing when independent, otherwise per index.
            slot = f"{index}:{iv}" if cfg.independent else index
            trade = trades.get(slot)
            need = (cfg.streak_for(index, iv), cfg.move_for(index, iv) * vol_scale)
            if not signals.qualifies(streak, *need):
                continue
            # Enter only on the candle that first makes the streak qualify. If it already qualified
            # a candle ago (missed run, first run mid-session), the entry price is stale: skip it.
            earlier = signals.current_streak(frame[:-1], cfg.flat_pct) if len(frame) > 1 else None
            if earlier and earlier.start_ts == streak.start_ts and signals.qualifies(earlier, *need):
                state["alerted"].append(key)
                continue
            if not _filters_pass(streak.direction, c5, mod, cfg):
                continue
            state["alerted"].append(key)
            if trade and trade["direction"] == streak.direction:
                trade["confirmed_by"].append(iv)
                alerts.append({"type": "CONFIRM", "index": index, "interval": iv, "key": key})
                continue
            if trade:  # opposite signal: close the old trade, then enter the new one
                alerts.append(_exit(trade, c5[-1].close, "opposite_signal", now_ts))
            trade = open_trade(index, iv, streak.direction, c5, frame, now_ts, cfg, ctx, key, streak)
            trades[slot] = trade
            alerts.append({"type": "ENTRY", "index": index, "interval": iv, "key": key,
                           "trigger_end_ts": frame[-1].ts + iv * 60, "trade": dict(trade)})
    return state, alerts
