"""Backtest the streak rules with the real engine, bar by bar.

For every configuration it reports trades after costs next to a baseline: entries in the same
direction, at the same time of day, on random other days, under the same exit rules. Settings are
chosen on the earlier ("fit") days and then judged on the later ("hold-out") days they never saw.

Results are in INDEX POINTS, not option profit. Costs are rough stand-ins (see COST_*).
"""
import argparse
import json
import random
import statistics
import time
from dataclasses import replace
from pathlib import Path

import archive
import engine
from candles import aggregate, fetch_yahoo, ist_mod
from config import COST_PER_HOUR, COST_ROUND_TRIP, INDICES, Config, load_config

BASE_DIR = Path(__file__).resolve().parent
MOVE_SCALE = {"NIFTY": 1.0, "BANKNIFTY": 1.3}
GRID = {
    5: {"streak": (5, 6, 8), "move": (0.20, 0.30), "hold": (3, 6, None)},
    15: {"streak": (3, 4, 5), "move": (0.25, 0.35), "hold": (2, 4, None)},
}
BASELINE_SAMPLES = 5
MIN_FIT_TRADES = 10
MIN_HOLDOUT_TRADES = 8


def make_cfg(index, iv, streak, move, hold, **filters) -> Config:
    return replace(
        Config(),
        intervals=(iv,),
        enabled=None,
        independent=False,
        min_streak={iv: streak},
        min_move_pct={index: {iv: round(move * MOVE_SCALE[index], 4)}},
        hold_candles={iv: hold},
        exit_at_hold=hold is not None,
        **filters,
    )


def run_day(index, c5, cfg, ctx=None) -> list[dict]:
    state, trades = {}, []
    for i, c in enumerate(c5):
        state, alerts = engine.step(state, {index: c5[: i + 1]}, c.ts + 300, cfg, ctx)
        trades += [a for a in alerts if a["type"] == "EXIT"]
    return trades


def run_forced(index, c5, cfg, entry_mod, direction, interval) -> dict | None:
    """Baseline trade: enter at entry_mod regardless of any signal; same exit rules."""
    cfg = replace(cfg, allow_entries=False)
    start = next((i for i, c in enumerate(c5) if ist_mod(c.ts + 300) >= entry_mod), None)
    if start is None or ist_mod(c5[start].ts + 300) >= cfg.hard_exit:
        return None
    prefix, now = c5[: start + 1], c5[start].ts + 300
    state, _ = engine.step({}, {index: prefix}, now, cfg)
    state["trades"][index] = engine.open_trade(index, interval, direction, prefix, aggregate(prefix, interval), now, cfg, None)
    for i in range(start + 1, len(c5)):
        state, alerts = engine.step(state, {index: c5[: i + 1]}, c5[i].ts + 300, cfg)
        for a in alerts:
            if a["type"] == "EXIT":
                return a
    return None


def net_points(trade) -> float:
    index = trade["index"]
    hours = (trade["exit_ts"] - trade["entry_ts"]) / 3600
    return trade["points"] - COST_ROUND_TRIP[index] - COST_PER_HOUR[index] * hours


def summarize(trades, n_days) -> dict:
    if not trades:
        return {"n": 0, "per_day": 0.0}
    net = [net_points(t) for t in trades]
    wins, losses = [x for x in net if x > 0], [x for x in net if x <= 0]
    return {
        "n": len(trades),
        "per_day": len(trades) / n_days,
        "win_pct": 100 * len(wins) / len(net),
        "gross": statistics.mean(t["points"] for t in trades),
        "net": statistics.mean(net),
        "median": statistics.median(net),
        "avg_win": statistics.mean(wins) if wins else 0.0,
        "avg_loss": statistics.mean(losses) if losses else 0.0,
        "worst": min(net),
        "hold_min": statistics.mean((t["exit_ts"] - t["entry_ts"]) / 60 for t in trades),
    }


def evaluate(index, iv, cfg, days: dict, ctxs: dict, seed: int) -> dict:
    """Run cfg over the given days. Returns stats plus the baseline's stats."""
    rng = random.Random(seed)
    names = list(days)
    trades, base = [], []
    for day in names:
        trades += [dict(t, day=day) for t in run_day(index, days[day], cfg, ctxs.get(day))]
    for t in trades:
        others = [d for d in names if d != t["day"]]
        for day in rng.sample(others, min(BASELINE_SAMPLES, len(others))):
            b = run_forced(index, days[day], cfg, ist_mod(t["entry_ts"]), t["direction"], iv)
            if b:
                base.append(b)
    stats = summarize(trades, len(names))
    stats["base"] = summarize(base, len(names))
    return stats


def avg_range_pct(c5) -> float:
    return statistics.mean((c.high - c.low) / c.open * 100 for c in c5)


def first_hour_range_pct(c5) -> float:
    first = [c for c in c5 if ist_mod(c.ts) < engine.FIRST_HOUR_END]
    return (max(c.high for c in first) - min(c.low for c in first)) / first[0].open * 100


def vol_contexts(days: dict) -> dict:
    """Per day: recent volatility (avg 5m range of the prior 5 sessions) relative to its median."""
    names = list(days)
    recent = {}
    for i, day in enumerate(names):
        if i >= 5:
            recent[day] = statistics.mean(avg_range_pct(days[d]) for d in names[i - 5:i])
    med = statistics.median(recent.values()) if recent else 1.0
    return {day: {"vol_scale": value / med} for day, value in recent.items()}


def fmt(s) -> str:
    if not s["n"]:
        return f"{'0':>4}  (no trades)"
    b = s.get("base", {"n": 0})
    base = f"{b['net']:>+7.1f} {b['win_pct']:>4.0f}%" if b["n"] else f"{'-':>7} {'-':>5}"
    edge = f"{s['net'] - b['net']:>+7.1f}" if b["n"] else f"{'-':>7}"
    return (f"{s['n']:>4} {s['per_day']:>5.2f} {s['win_pct']:>4.0f}% {s['gross']:>+7.1f} {s['net']:>+7.1f} "
            f"{s['avg_win']:>+7.1f} {s['avg_loss']:>+7.1f} {s['worst']:>+8.1f} {s['hold_min']:>5.0f} |{base} {edge}")


HEADER = (f"{'n':>4} {'/day':>5} {'win':>5} {'gross':>7} {'net':>7} {'avgwin':>7} {'avgloss':>7} {'worst':>8} "
          f"{'mins':>5} |{'basenet':>7} {'bwin':>5} {'edge':>7}")


def label(streak, move, hold) -> str:
    return f"streak>={streak} move>={move:.2f}% hold={'trail' if hold is None else hold}"


def load_history(index, refresh=True) -> dict:
    if refresh:
        try:
            archive.save_days(index, fetch_yahoo(index, rng="60d"))
        except Exception as error:
            print(f"  warning: could not refresh {index} from Yahoo ({type(error).__name__}); using archive only")
    return archive.load_days(index)


def is_go(hold) -> bool:
    return (hold["n"] >= MIN_HOLDOUT_TRADES and hold["net"] > 0 and hold["base"]["n"] > 0
            and hold["net"] > hold["base"]["net"])


def write_hold_stats(holdout: float, refresh: bool) -> dict:
    """History line for alerts: the live settings' record on the fit days only (never the hold-out)."""
    live, out = load_config(), {}
    for index, ivs in live.enabled.items():
        days = load_history(index, refresh)
        names = list(days)
        fit = {d: days[d] for d in names[: int(len(names) * (1 - holdout))]}
        for iv in ivs:
            hold = live.hold_for(index, iv)
            cfg = replace(live, intervals=(iv,), enabled=None, exit_at_hold=hold is not None)
            s = evaluate(index, iv, cfg, fit, {}, seed=1)
            out[f"{index}:{iv}"] = {
                "hold_candles": hold, "days": len(fit), "n": s["n"],
                "win_pct": s.get("win_pct"), "net": s.get("net"),
                "base_win_pct": s["base"].get("win_pct"), "base_net": s["base"].get("net"),
            }
    (BASE_DIR / "hold_stats.json").write_text(json.dumps(out, indent=2))
    return out


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--no-refresh", action="store_true", help="use archived candles only")
    parser.add_argument("--holdout", type=float, default=0.25, help="share of days held out (default 0.25)")
    parser.add_argument("--write-hold-stats", action="store_true",
                        help="write hold_stats.json for the live settings in config.py and exit")
    args = parser.parse_args()
    if args.write_hold_stats:
        print(json.dumps(write_hold_stats(args.holdout, not args.no_refresh), indent=2))
        return
    started = time.time()
    results, verdicts = {}, []

    for index in INDICES:
        days = load_history(index, refresh=not args.no_refresh)
        names = list(days)
        cut = int(len(names) * (1 - args.holdout))
        fit = {d: days[d] for d in names[:cut]}
        hold = {d: days[d] for d in names[cut:]}
        ctxs = vol_contexts(days)
        quiet = sorted(first_hour_range_pct(c) for c in fit.values())[len(fit) // 3]
        print(f"\n{'=' * 118}\n{index}: {len(names)} days ({names[0]} to {names[-1]}). "
              f"Fit {len(fit)} days, hold-out {len(hold)} days (from {names[cut]}).")
        print(f"Costs per trade: {COST_ROUND_TRIP[index]} pts + {COST_PER_HOUR[index]} pts per hour held. "
              f"Move thresholds x{MOVE_SCALE[index]}.")

        for iv, grid in GRID.items():
            print(f"\n--- {index} {iv}m, fit days ---\n{'config':<36} {HEADER}")
            rows = []
            for streak in grid["streak"]:
                for move in grid["move"]:
                    for hold_c in grid["hold"]:
                        cfg = make_cfg(index, iv, streak, move, hold_c)
                        stats = evaluate(index, iv, cfg, fit, {}, seed=1)
                        rows.append(((streak, move, hold_c), stats))
                        print(f"{label(streak, move, hold_c):<36} {fmt(stats)}")
            eligible = [r for r in rows if r[1]["n"] >= MIN_FIT_TRADES]
            if not eligible:
                print(f"No configuration produced {MIN_FIT_TRADES}+ trades on the fit days.")
                verdicts.append((index, iv, None, None))
                continue
            (streak, move, hold_c), fit_stats = max(eligible, key=lambda r: r[1]["net"])
            best = label(streak, move, hold_c)
            variants = {
                "plain": ({}, {}),
                "with day's direction": ({"require_day_direction": True}, {}),
                "before 11:30 only": ({"entry_window": (0, 11 * 60 + 30)}, {}),
                "after 13:30 only": ({"entry_window": (13 * 60 + 30, 24 * 60)}, {}),
                "skip quiet days": ({"min_first_hour_range_pct": quiet}, {}),
                "volatility-scaled move": ({}, ctxs),
            }
            print(f"\nBest on fit days: {best}. Variants (each alone):\n{'variant':<25} {'days':<8} {HEADER}")
            for name, (filters, ctx) in variants.items():
                cfg = make_cfg(index, iv, streak, move, hold_c, **filters)
                f_stats = fit_stats if name == "plain" else evaluate(index, iv, cfg, fit, ctx, seed=1)
                h_stats = evaluate(index, iv, cfg, hold, ctx, seed=2)
                print(f"{name:<25} {'fit':<8} {fmt(f_stats)}")
                print(f"{'':<25} {'HOLD-OUT':<8} {fmt(h_stats)}")
                results[f"{index}:{iv}:{name}"] = {"config": best, "streak": streak, "move_pct": cfg.min_move_pct[index][iv],
                                                   "hold_candles": hold_c, "fit": f_stats, "holdout": h_stats}
                if name == "plain":
                    verdicts.append((index, iv, best, h_stats))

    print(f"\n{'=' * 118}\nVERDICT (plain rule, settings chosen on fit days, judged on hold-out days, after costs)")
    any_go = False
    for index, iv, best, h in verdicts:
        if not h or not h["n"]:
            print(f"  {index} {iv}m: NO-GO (too few trades to judge)")
            continue
        go = is_go(h)
        any_go |= go
        why = (f"{h['n']} trades, net {h['net']:+.1f} pts/trade vs baseline "
               f"{h['base']['net']:+.1f}" if h["base"]["n"] else f"{h['n']} trades, net {h['net']:+.1f} pts/trade")
        print(f"  {index} {iv}m [{best}]: {'GO' if go else 'NO-GO'} ({why})")
    print(f"\nOverall: {'GO for at least one combination' if any_go else 'NO-GO'}. "
          f"Samples are small; treat every figure as rough. ({time.time() - started:.0f}s)")
    (BASE_DIR / "backtest_results.json").write_text(json.dumps(results, indent=2))


if __name__ == "__main__":
    main()
