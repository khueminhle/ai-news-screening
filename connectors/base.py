"""Shared pieces for every connector.

- load_watchlist(): reads watchlist/watchlist.json. Every connector uses it.
- Item: one normalised record (a filing, a news story, ...).
- Connector: base class. Each vendor implements fetch(day) -> list[Item].
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent
WATCHLIST_PATH = PROJECT_ROOT / "watchlist" / "watchlist.json"
RAW_DIR = PROJECT_ROOT / "data" / "raw"


# ---------- one time format for the whole project ----------
# Moments in time:  "YYYY-MM-DDTHH:MM:SSZ" in UTC.
# Calendar dates:   "YYYY-MM-DD" (trading day, filing date, observation date) - never converted.

def utc_now_z() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def to_utc_z(value) -> str | None:
    """Any timestamp -> 'YYYY-MM-DDTHH:MM:SSZ' (UTC). Plain dates stay 'YYYY-MM-DD'.

    Accepts: Unix seconds (int/float), ISO strings with 'Z' or an offset,
    'YYYY-MM-DD HH:MM:SS' (treated as UTC), datetime objects, and plain dates.
    """
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        dt = datetime.fromtimestamp(value, tz=timezone.utc)
    elif isinstance(value, datetime):
        dt = value
    elif isinstance(value, date):
        return value.isoformat()
    else:
        text = str(value).strip()
        if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
            return text
        dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def clean_headline(title: str | None) -> str:
    """Rule-based headline cleaning used to spot the same story (done by code, not Claude).

    lowercase -> drop a trailing ' - Publisher' -> drop punctuation -> collapse spaces.
    """
    text = (title or "").strip()
    text = re.sub(r"\s+[-–—|]\s+[^-–—|]{2,60}$", "", text)
    text = re.sub(r"[^\w\s]", " ", text.lower())
    return re.sub(r"\s+", " ", text).strip()


def write_items_json(raw_dir: Path, day: date, items: list) -> Path:
    out = Path(raw_dir) / day.isoformat() / "items.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([it.to_dict() for it in items], indent=2, ensure_ascii=False), encoding="utf-8")
    return out


def load_watchlist_entries(path: Path = WATCHLIST_PATH) -> list[dict]:
    """Return watchlist entries as [{"ticker": "NET", "name": "Cloudflare" or None, "news_query": ... or None}, ...].

    Accepts {"tickers": [...]} or a bare list. Each entry is a ticker string or an
    object with "ticker", an optional "name" and an optional "news_query" (the words the RSS
    news search uses instead of the name). Upper-cased, de-duplicated, file order.
    """
    with open(path, encoding="utf-8") as f:
        data = json.load(f)

    entries = data.get("tickers") if isinstance(data, dict) else data
    if not isinstance(entries, list) or not entries:
        raise ValueError(f"{path} must contain a non-empty 'tickers' list")

    seen: set[str] = set()
    result: list[dict] = []
    for raw in entries:
        if isinstance(raw, dict):
            ticker, name, query = str(raw.get("ticker", "")), raw.get("name"), raw.get("news_query")
        else:
            ticker, name, query = str(raw), None, None
        ticker = ticker.strip().upper()
        if ticker and ticker not in seen:
            seen.add(ticker)
            result.append({"ticker": ticker, "name": (str(name).strip() or None) if name else None,
                           "news_query": (str(query).strip() or None) if query else None})
    return result


def load_watchlist(path: Path = WATCHLIST_PATH) -> list[str]:
    """Return just the tickers in the watchlist. No size limit."""
    return [e["ticker"] for e in load_watchlist_entries(path)]


@dataclass
class Item:
    source: str              # connector name, e.g. "sec_edgar"
    source_id: str           # the vendor's unique ID (SEC: accession number)
    tickers: list[str]
    title: str
    url: str
    published_at: str        # ISO date or datetime
    item_type: str           # e.g. "8-K", "news"
    extra: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)


class Connector:
    name = "base"

    def __init__(self, tickers: list[str] | None = None):
        self.tickers = tickers if tickers is not None else load_watchlist()
        self.errors: list[str] = []

    def fetch(self, day: date) -> list[Item]:
        raise NotImplementedError
