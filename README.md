# trend_watch

Watches Nifty 50 and BankNifty for runs of same-direction candles and sends a Telegram alert.
A rising run suggests **buying a call (CE)**, a falling run **buying a put (PE)**. It never
suggests selling options and never places orders.

**Status: paper mode.** The backtest (2026-11-20) was inconclusive: two of four pairings passed
the gate but neither is statistically distinguishable from luck. Alerts are tagged `PAPER` and every
outcome is journaled so the rule can be judged on forward data before any real money is used.

## What it alerts on

All four index and candle-size pairings run side by side, each as its own paper trade, so the
journal can show which (if any) holds up. Each uses the setting that scored best on the
backtest's first 44 days.

| Pairing | Minimum run | Minimum net move | Suggested hold | Passed on unseen days? |
|---|---|---|---|---|
| BankNifty 5m | 5 candles | 0.39% | 15 min | yes |
| Nifty 15m | 4 candles | 0.35% | no fixed time; trail the stop | yes |
| Nifty 5m | 5 candles | 0.30% | 30 min | no |
| BankNifty 15m | 5 candles | 0.455% | 60 min | no (only 4 trades) |

- **Up candle:** close above open. **Flat** (body under 0.01%) neither counts nor breaks a run.
- **Net move:** first candle's open to last candle's close.
- **Stop:** low of the latest rising candle (high for a falling run). It only tightens. It is hit
  when a candle of the same size *closes* beyond it.
- Runs reset each morning. No entries after 14:30 IST. Everything exits at 15:15 IST.
- Each pairing holds at most one trade and ignores the others, so an index can have a 5m and a
  15m trade open at once. Every alert names its candle size. (`independent = False` in
  `config.py` switches to one trade per index, with the other timeframe only confirming.)

Alerts: `ENTRY`, `EXIT`, a "suggested hold reached" note, and a once-a-day warning if both data
sources (Groww, then Yahoo) fail.

## Commands

```
python3 trend_watch.py --dry-run            # evaluate now, print, send nothing
python3 trend_watch.py --replay 2026-11-19  # what would have fired on a past day
python3 trend_watch.py --summary 30         # journal results, last 30 days
python3 trend_watch.py --test-alert         # check Telegram delivery
python3 backtest.py                         # full backtest (about 25 seconds)
python3 backtest.py --write-hold-stats      # refresh the "History" line used in alerts
python3 -m unittest discover tests
```

## Setup

1. `cp .env.example .env` and fill in `TELEGRAM_BOT_TOKEN` and `TELEGRAM_CHAT_ID`.
2. `python3 trend_watch.py --test-alert`
3. `./install_cron.sh` adds its own block to the crontab (every 5 minutes in market hours, plus
   a daily archive run), keeps a backup of the old crontab, and prints the diff.

Settings live in `config.py`; each can be overridden in `.env` (see `.env.example`). Set
`TREND_WATCH_PAPER=0` to drop the PAPER tag.

## Files

| File | Purpose |
|---|---|
| `candles.py` | Fetch 5m candles, build 15m candles from them |
| `signals.py`, `engine.py` | The rules. Live runs, replay and the backtest share this code |
| `trend_watch.py` | Entry point: fetch, decide, send, journal |
| `backtest.py` | Tests the rules against a baseline, with costs and hold-out days |
| `journal.csv` | One row per entry and exit |
| `archive/` | One file of 5m candles per index per day; grows daily |
| `hold_stats.json` | Backtest record quoted in entry alerts |

## Reading the results

All figures are **index points, not option profit**. The cost estimates (Nifty 4 points plus 2
per hour held; BankNifty 10 plus 5 per hour) are rough stand-ins for spread, fees and premium
decay. In `--summary`, "as tested" exits at the suggested hold time where there is one, which is
the rule the backtest measured; "trailed" stays in until the stop.

After 4-6 weeks, compare `--summary` with the backtest and rerun `backtest.py` on the larger archive.
