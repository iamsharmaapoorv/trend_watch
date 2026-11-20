"""Per-day store of 5m candles, so history grows beyond what the free sources serve."""
import json
from pathlib import Path

from candles import SESSION_CLOSE, SESSION_OPEN, Candle, ist_day, ist_mod, ist_str

ARCHIVE_DIR = Path(__file__).resolve().parent / "archive"


def split_days(candles: list[Candle]) -> dict[str, list[Candle]]:
    """Group session candles (09:15-15:30 IST) by IST date string."""
    days: dict[str, list[Candle]] = {}
    for c in sorted(candles, key=lambda c: c.ts):
        if SESSION_OPEN <= ist_mod(c.ts) < SESSION_CLOSE:
            days.setdefault(ist_str(c.ts, "%Y-%m-%d"), []).append(c)
    return days


def save_days(index: str, candles: list[Candle], directory: Path = ARCHIVE_DIR, min_candles: int = 70) -> int:
    """Write one file per complete day. Existing files are left alone. Returns files written."""
    directory.mkdir(exist_ok=True)
    written = 0
    for day, rows in split_days(candles).items():
        path = directory / f"{index}-{day}.json"
        if path.exists() or len(rows) < min_candles:
            continue
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps([[c.ts, c.open, c.high, c.low, c.close] for c in rows]))
        tmp.replace(path)
        written += 1
    return written


def load_days(index: str, directory: Path = ARCHIVE_DIR) -> dict[str, list[Candle]]:
    out = {}
    for path in sorted(directory.glob(f"{index}-*.json")):
        out[path.stem[len(index) + 1:]] = [Candle(*row) for row in json.loads(path.read_text())]
    return out
