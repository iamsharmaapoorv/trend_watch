"""Candle fetching, parsing and aggregation. All timestamps are Unix seconds (UTC), candle start."""
import logging
from dataclasses import dataclass
from datetime import datetime, timezone

import requests

IST_OFFSET = 19800  # +05:30
FIVE_MIN = 300
SESSION_OPEN = 9 * 60 + 15  # minutes after IST midnight
SESSION_CLOSE = 15 * 60 + 30
USER_AGENT = {"User-Agent": "Mozilla/5.0"}
TIMEOUT = 15

GROWW_URL = "https://groww.in/v1/api/charting_service/v2/chart/exchange/NSE/segment/CASH/{symbol}"
YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"
YAHOO_SYMBOLS = {"NIFTY": "%5ENSEI", "BANKNIFTY": "%5ENSEBANK"}


class DataError(RuntimeError):
    pass


@dataclass(frozen=True)
class Candle:
    ts: int
    open: float
    high: float
    low: float
    close: float


def ist_day(ts: int) -> int:
    """Day number in IST (days since epoch)."""
    return (ts + IST_OFFSET) // 86400


def ist_mod(ts: int) -> int:
    """Minute of the IST day."""
    return ((ts + IST_OFFSET) % 86400) // 60


def ist_ts(day: int, minute_of_day: int) -> int:
    return day * 86400 - IST_OFFSET + minute_of_day * 60


def ist_str(ts: int, fmt: str = "%Y-%m-%d %H:%M") -> str:
    return datetime.fromtimestamp(ts + IST_OFFSET, timezone.utc).strftime(fmt)


def parse_groww(payload: dict) -> list[Candle]:
    out = []
    for row in payload.get("candles") or []:
        ts, o, h, l, c = row[:5]
        if None in (ts, o, h, l, c):
            continue
        out.append(Candle(int(ts), float(o), float(h), float(l), float(c)))
    return sorted(out, key=lambda c: c.ts)


def parse_yahoo(payload: dict) -> list[Candle]:
    result = payload["chart"]["result"][0]
    quote = result["indicators"]["quote"][0]
    out = []
    for ts, o, h, l, c in zip(result.get("timestamp") or [], quote["open"], quote["high"], quote["low"], quote["close"]):
        if None in (ts, o, h, l, c):
            continue
        if ts % FIVE_MIN:  # Yahoo appends an off-grid live tick; it is not a candle
            continue
        out.append(Candle(int(ts), float(o), float(h), float(l), float(c)))
    return sorted(out, key=lambda c: c.ts)


def _get_json(url: str, params: dict) -> dict:
    res = requests.get(url, params=params, headers=USER_AGENT, timeout=TIMEOUT)
    res.raise_for_status()
    return res.json()


def fetch_groww(symbol: str, now_ts: int, days: int = 4) -> list[Candle]:
    params = {
        "intervalInMinutes": 5,
        "startTimeInMillis": (now_ts - days * 86400) * 1000,
        "endTimeInMillis": now_ts * 1000,
    }
    return parse_groww(_get_json(GROWW_URL.format(symbol=symbol), params))


def fetch_yahoo(symbol: str, now_ts: int = 0, rng: str = "5d") -> list[Candle]:
    url = YAHOO_URL.format(symbol=YAHOO_SYMBOLS[symbol])
    return parse_yahoo(_get_json(url, {"interval": "5m", "range": rng}))


def fetch_5m(symbol: str, now_ts: int, fetchers=None) -> tuple[list[Candle], str]:
    """Try each source in order; return (candles, source_name). Raises DataError if all fail."""
    fetchers = fetchers or (("groww", fetch_groww), ("yahoo", fetch_yahoo))
    for name, fetch in fetchers:
        try:
            candles = fetch(symbol, now_ts)
        except Exception as error:
            logging.warning(f"{name} fetch failed for {symbol}: {type(error).__name__}: {error}")
            continue
        if candles:
            return candles, name
        logging.warning(f"{name} returned no candles for {symbol}")
    raise DataError(f"all data sources failed for {symbol}")


def session_completed(candles: list[Candle], now_ts: int) -> list[Candle]:
    """Completed 5m candles from today's session (IST date of now_ts), 09:15-15:30."""
    today = ist_day(now_ts)
    return [
        c
        for c in candles
        if ist_day(c.ts) == today
        and SESSION_OPEN <= ist_mod(c.ts) < SESSION_CLOSE
        and c.ts + FIVE_MIN <= now_ts
    ]


def aggregate(candles_5m: list[Candle], minutes: int) -> list[Candle]:
    """Build larger candles from completed 5m candles. Buckets align to 09:15 IST.
    A bucket is emitted only once its last 5m slot has completed."""
    if minutes == 5:
        return list(candles_5m)
    if not candles_5m:
        return []
    size = minutes * 60
    data_end = candles_5m[-1].ts + FIVE_MIN
    buckets: dict[int, list[Candle]] = {}
    for c in candles_5m:
        buckets.setdefault(c.ts - c.ts % size, []).append(c)
    out = []
    for start in sorted(buckets):
        if start + size > data_end:
            continue
        group = buckets[start]
        out.append(Candle(start, group[0].open, max(c.high for c in group), min(c.low for c in group), group[-1].close))
    return out
