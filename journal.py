"""Trade journal: one CSV row per ENTRY and per EXIT, plus a summary of closed trades."""
import csv
import statistics
from pathlib import Path

from candles import ist_str
from config import COST_PER_HOUR, COST_ROUND_TRIP

FIELDS = ["time_ist", "event", "index", "interval", "direction", "price", "entry_time_ist",
          "entry_price", "points", "est_cost", "rule_points", "rule_cost", "reason", "paper"]


def _append(path: Path, row: dict) -> None:
    new = not Path(path).exists()
    with open(path, "a", newline="", encoding="utf-8") as fh:
        writer = csv.DictWriter(fh, fieldnames=FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(row)


def record_entry(path, trade: dict, paper: bool) -> None:
    _append(path, {
        "time_ist": ist_str(trade["entry_ts"]), "event": "ENTRY", "index": trade["index"],
        "interval": trade["interval"], "direction": "CE" if trade["direction"] > 0 else "PE",
        "price": trade["entry_price"], "paper": int(paper),
    })


def record_exit(path, alert: dict, paper: bool) -> None:
    index = alert["index"]
    hours = (alert["exit_ts"] - alert["entry_ts"]) / 3600
    cost = COST_ROUND_TRIP[index] + COST_PER_HOUR[index] * hours
    # "rule" = the rule as backtested: out at the suggested hold time if that came first.
    rule_points, rule_cost = alert["points"], cost
    if alert.get("hold_points") is not None:
        rule_points = alert["hold_points"]
        rule_cost = COST_ROUND_TRIP[index] + COST_PER_HOUR[index] * (alert["hold_ts"] - alert["entry_ts"]) / 3600
    _append(path, {
        "time_ist": ist_str(alert["exit_ts"]), "event": "EXIT", "index": index,
        "interval": alert["interval"], "direction": "CE" if alert["direction"] > 0 else "PE",
        "price": alert["exit_price"], "entry_time_ist": ist_str(alert["entry_ts"]),
        "entry_price": alert["entry_price"], "points": round(alert["points"], 2),
        "est_cost": round(cost, 2), "rule_points": round(rule_points, 2), "rule_cost": round(rule_cost, 2),
        "reason": alert["reason"], "paper": int(paper),
    })


def summary(path, since_day: str | None = None) -> dict:
    """Closed trades per pairing ("NIFTY 15m"). `since_day` is an IST date string (inclusive)."""
    out = {}
    if not Path(path).exists():
        return out
    with open(path, newline="", encoding="utf-8") as fh:
        rows = [r for r in csv.DictReader(fh) if r["event"] == "EXIT" and (not since_day or r["time_ist"][:10] >= since_day)]
    pairing = lambda r: f"{r['index']} {r['interval']}m"
    for index in sorted({pairing(r) for r in rows}):
        mine = [r for r in rows if pairing(r) == index]
        net = [float(r["rule_points"]) - float(r["rule_cost"]) for r in mine]  # as backtested
        trailed = [float(r["points"]) - float(r["est_cost"]) for r in mine]  # staying in until the stop
        out[index] = {
            "trades": len(mine), "win_pct": 100 * sum(x > 0 for x in net) / len(net),
            "total_net": sum(net), "avg_net": statistics.mean(net),
            "trailed_total_net": sum(trailed), "trailed_avg_net": statistics.mean(trailed),
        }
    return out
