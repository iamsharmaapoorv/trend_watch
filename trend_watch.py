#!/usr/bin/env python3
"""Nifty / BankNifty trend-streak monitor. Sends Telegram alerts; never places orders.

Usage:
  trend_watch.py                 one live run (what cron calls)
  trend_watch.py --dry-run       fetch and evaluate, print alerts, send and save nothing
  trend_watch.py --replay DAY    step through a past day (YYYY-MM-DD) and print its alerts
  trend_watch.py --summary [N]   journal results for the last N days (default 30)
  trend_watch.py --test-alert    send a test message to Telegram
  trend_watch.py --archive       save recent complete days of candles to archive/
"""
import argparse
import json
import logging
import time
from pathlib import Path

import archive
import engine
import journal
from candles import (FIVE_MIN, SESSION_CLOSE, SESSION_OPEN, fetch_groww, fetch_yahoo, ist_mod, ist_str, ist_ts,
                     ist_day, session_completed)
from config import INDICES, Config, load_config
from logging_utils import setup_logging
from state import StateStore
from telegram_alert import TelegramAlert

BASE_DIR = Path(__file__).resolve().parent
STATE_PATH = BASE_DIR / ".trend_watch_state.json"
JOURNAL_PATH = BASE_DIR / "journal.csv"
HOLD_STATS_PATH = BASE_DIR / "hold_stats.json"
SOURCES = (("groww", fetch_groww), ("yahoo", fetch_yahoo))
RUN_FROM = SESSION_OPEN + 5  # first 5m candle has closed
STALE_AFTER = 2 * FIVE_MIN  # latest completed candle may lag this much
ENTRY_MAX_AGE = FIVE_MIN  # an entry older than this is sent at a stale price, so it is dropped


def n(x: float) -> str:
    return f"{x:,.0f}"


def idea(direction: int) -> str:
    return "buy CE" if direction > 0 else "buy PE"


def build_message(alert: dict, cfg: Config, hold_stats: dict | None = None) -> str:
    tag = "PAPER · " if cfg.paper else ""
    kind = alert["type"]
    if kind == "DATA_DOWN":
        return (f"⚠️ trend_watch: no fresh data for {', '.join(alert['indices'])} from Groww or Yahoo. "
                "No signals until data returns.")
    index = alert["index"]
    if kind == "ENTRY":
        t = alert["trade"]
        d, iv = t["direction"], t["interval"]
        rising = d > 0
        lines = [
            f"{'🟢' if rising else '🔻'} {tag}{index} {'rising' if rising else 'falling'} — {t['streak_len']} × {iv}m candles, "
            f"{t['net_pct']:+.2f}% ({n(t['streak_start_price'])} → {n(t['entry_price'])})",
            f"Idea: {idea(d)}",
        ]
        if t["hold_candles"] is None:
            lines.append("Hold: no fixed time; stay in while the stop holds.")
        else:
            mins = round((t["hold_until"] - t["entry_ts"]) / 60)
            lines.append(f"Hold ≈ {mins} min (to ~{ist_str(t['hold_until'], '%H:%M')} IST).")
        lines.append(
            f"Stop {n(t['stop'])}, trailing to the {'low' if rising else 'high'} of each new "
            f"{'rising' if rising else 'falling'} {iv}m candle; exit on a {iv}m close "
            f"{'below' if rising else 'above'} it. Hard exit {cfg.hard_exit // 60}:{cfg.hard_exit % 60:02d}."
        )
        s = (hold_stats or {}).get(f"{index}:{iv}")
        if s and s.get("n"):
            lines.append(
                f"History ({s['days']} days, rough): {s['n']} similar signals, {s['win_pct']:.0f}% profitable "
                f"vs {s['base_win_pct']:.0f}% baseline, avg {s['net']:+.0f} pts after costs."
            )
        return "\n".join(lines)
    if kind == "EXIT":
        reasons = {
            "stop": f"stop hit ({alert['interval']}m close beyond the stop)",
            "opposite_signal": "opposite signal",
            "session_end": "end of session",
            "hold": "hold time reached",
        }
        return (
            f"🔴 {tag}EXIT {index} {alert['interval']}m ({idea(alert['direction'])}) — {reasons.get(alert['reason'], alert['reason'])}\n"
            f"Entry {n(alert['entry_price'])} at {ist_str(alert['entry_ts'], '%H:%M')} → "
            f"exit {n(alert['exit_price'])} at {ist_str(alert['exit_ts'], '%H:%M')}: "
            f"{alert['points']:+.0f} index pts."
        )
    if kind == "TIME_UP":
        t = alert["trade"]
        pts = (alert["price"] - t["entry_price"]) * t["direction"]
        return (
            f"⏰ {tag}{index} {t['interval']}m ({idea(t['direction'])}): suggested hold reached, stop not hit. "
            f"Now {n(alert['price'])} ({pts:+.0f} pts). Stop {n(t['stop'])}; trail it or take profit."
        )
    raise ValueError(f"unknown alert type {kind}")


def gather(now_ts: int, sources=SOURCES, indices=INDICES):
    """Fetch 5m candles per index, falling back when a source errors or lags.
    Returns (candles_by_index, down): `down` lists indices with no usable data although the
    market should be open. An index whose sources work but show no session today (holiday) is
    simply omitted."""
    out, down = {}, []
    for index in indices:
        failed = False
        for name, fetch in sources:
            try:
                candles = fetch(index, now_ts)
            except Exception as error:
                logging.warning(f"{index}: {name} fetch failed: {type(error).__name__}")
                failed = True
                continue
            today = session_completed(candles, now_ts)
            if not today:
                logging.info(f"{index}: {name} has no candles for today's session")
                continue
            lag = now_ts - (today[-1].ts + FIVE_MIN)
            if lag > STALE_AFTER:
                logging.warning(f"{index}: {name} is stale, latest candle closed {lag}s ago")
                failed = True
                continue
            logging.info(f"{index}: {name}, {len(today)} candles today, latest closed {lag}s ago")
            out[index] = candles
            break
        else:
            if failed:
                down.append(index)
    return out, down


def run_once(now_ts, cfg, state, send, sources=SOURCES, journal_path=JOURNAL_PATH, hold_stats=None, retry_wait=3.0):
    """One live run. `send(text) -> bool`. Returns the new state."""
    mod = ist_mod(now_ts)
    if not (RUN_FROM <= mod <= SESSION_CLOSE):
        logging.info("Outside market hours; nothing to do.")
        return state

    def deliver(text: str) -> bool:
        if send(text):
            return True
        time.sleep(retry_wait)
        return send(text)

    # Messages that failed earlier (exits, warnings) go first, oldest first.
    state = dict(state)
    state["outbox"] = [text for text in state.get("outbox", []) if not deliver(text)]

    candles, down = gather(now_ts, sources)
    today = ist_str(now_ts, "%Y-%m-%d")
    queue = []
    if down and state.get("data_down_day") != today:
        state["data_down_day"] = today
        queue.append({"type": "DATA_DOWN", "indices": down})

    state, alerts = engine.step(state, candles, now_ts, cfg, {"hold_stats": hold_stats})
    for alert in queue + alerts:
        kind = alert["type"]
        if kind == "CONFIRM":
            logging.info(f"{alert['index']}: {alert['interval']}m streak confirms the open trade (not sent).")
            continue
        text = build_message(alert, cfg, hold_stats)
        if kind == "ENTRY":
            index, trade = alert["index"], alert["trade"]
            age = now_ts - alert["trigger_end_ts"]
            if age <= ENTRY_MAX_AGE and deliver(text):
                journal.record_entry(journal_path, trade, cfg.paper)
                logging.info(f"ENTRY sent: {index} {trade['interval']}m dir {trade['direction']}")
            else:
                # Undelivered or stale entries are dropped, not retried: the price has moved on.
                for slot in [s for s, t in state["trades"].items() if t["key"] == alert["key"]]:
                    del state["trades"][slot]
                logging.warning(f"ENTRY dropped for {index} (age {age}s, undelivered or stale).")
            continue
        if kind == "EXIT":
            journal.record_exit(journal_path, alert, cfg.paper)
        if deliver(text):
            logging.info(f"{kind} sent: {alert.get('index', '')}")
        else:
            state["outbox"].append(text)  # exits and warnings are retried until sent
            logging.warning(f"{kind} not delivered; will retry next run.")
    return state


def load_hold_stats() -> dict | None:
    try:
        return json.loads(HOLD_STATS_PATH.read_text())
    except (OSError, json.JSONDecodeError):
        return None


def replay(day: str, cfg: Config) -> None:
    """Step through a past day every 5 minutes and print what would have been sent."""
    by_index = {}
    for index in INDICES:
        days = archive.load_days(index)
        if day not in days:
            archive.save_days(index, fetch_yahoo(index, rng="60d"))
            days = archive.load_days(index)
        if day in days:
            by_index[index] = days[day]
    if not by_index:
        print(f"No candles for {day}.")
        return
    first = next(iter(by_index.values()))[0].ts
    start = ist_ts(ist_day(first), RUN_FROM)
    state, stats, count = {}, load_hold_stats(), 0
    for now in range(start, ist_ts(ist_day(first), SESSION_CLOSE) + 1, FIVE_MIN):
        state, alerts = engine.step(state, by_index, now, cfg, {"hold_stats": stats})
        for alert in alerts:
            if alert["type"] == "CONFIRM":
                continue
            count += 1
            print(f"\n[{ist_str(now, '%H:%M')} IST]\n{build_message(alert, cfg, stats)}")
    print(f"\n{count} alert(s) on {day}.")


def print_summary(days: int) -> None:
    since = ist_str(int(time.time()) - days * 86400, "%Y-%m-%d")
    result = journal.summary(JOURNAL_PATH, since)
    if not result:
        print(f"No closed trades in the journal since {since}.")
        return
    print(f"Closed trades since {since}, in index points after estimated costs.")
    print("'As tested' exits at the suggested hold time when there is one; 'trailed' stays in until the stop.")
    for index, s in result.items():
        print(f"  {index}: {s['trades']} trades, {s['win_pct']:.0f}% profitable. "
              f"As tested: total {s['total_net']:+.0f}, avg {s['avg_net']:+.1f}. "
              f"Trailed: total {s['trailed_total_net']:+.0f}, avg {s['trailed_avg_net']:+.1f}.")


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--replay", metavar="YYYY-MM-DD")
    parser.add_argument("--summary", nargs="?", const=30, type=int, metavar="DAYS")
    parser.add_argument("--test-alert", action="store_true")
    parser.add_argument("--archive", action="store_true")
    args = parser.parse_args()
    cfg = load_config()

    if args.replay:
        return replay(args.replay, cfg)
    if args.summary is not None:
        return print_summary(args.summary)

    setup_logging("trend_watch")
    now_ts = int(time.time())
    if args.test_alert:
        ok = TelegramAlert().send("trend_watch test alert: Telegram delivery works.")
        print("Delivered." if ok else "NOT delivered; see logs/trend_watch.log.")
        return
    if args.archive:
        for index in INDICES:
            try:
                written = archive.save_days(index, fetch_groww(index, now_ts, days=10))
            except Exception as error:
                logging.warning(f"Groww archive fetch failed for {index} ({type(error).__name__}); trying Yahoo.")
                written = archive.save_days(index, fetch_yahoo(index, rng="5d"))
            logging.info(f"Archived {written} new day(s) for {index}.")
        return
    if args.dry_run:
        logging.getLogger().handlers.clear()  # dry runs print to the terminal, not the log file
        logging.basicConfig(level=logging.INFO, format="[dry-run] %(message)s")

        def show(text):
            print(f"\n--- would send ---\n{text}")
            return True
        run_once(now_ts, cfg, StateStore(STATE_PATH).load(), show, journal_path=Path("/dev/null"),
                 hold_stats=load_hold_stats())
        return

    store = StateStore(STATE_PATH)
    state = run_once(now_ts, cfg, store.load(), TelegramAlert().send, hold_stats=load_hold_stats())
    store.save(state)


if __name__ == "__main__":
    main()
