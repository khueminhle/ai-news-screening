"""What Claude must reply with when classifying items. Python checks every reply against these."""
from __future__ import annotations

import json
import re
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field

EVENT_TYPES = (
    "earnings",            # results, earnings call, earnings date
    "guidance",            # outlook / forecast changes
    "analyst_rating",      # upgrade, downgrade, price target, initiation
    "product",             # launches, roadmap, technology
    "customer_partnership",
    "legal_regulatory",    # lawsuits, investigations, export rules, antitrust
    "m_and_a",             # acquisitions, mergers, divestitures
    "management",          # executive / board changes
    "capital_return",      # buybacks, dividends
    "financing",           # debt, equity offerings
    "insider_ownership",   # insider trades, big holders, 13D/G
    "periodic_report",     # 10-Q / 10-K filed
    "stock_move",          # article mainly about the share price moving
    "macro",               # central banks, rates, inflation, jobs, economy
    "market_commentary",   # broad market wrap / sector commentary
    "opinion",             # "should you buy", listicles, personal views
    "other",
)

EventType = Literal[EVENT_TYPES]  # type: ignore[valid-type]


class Figure(BaseModel):
    model_config = ConfigDict(extra="forbid")
    label: str = Field(min_length=1, max_length=80)     # "EPS", "revenue", "EPS estimate"
    value: str = Field(min_length=1, max_length=40)     # as written: "$5.32", "$1.026B", "12%"
    quote: str = Field(min_length=1, max_length=300)    # exact words from the item that contain the value


class ItemLabel(BaseModel):
    model_config = ConfigDict(extra="forbid")
    item_id: int
    relevant: bool
    tickers: list[str]
    event_type: EventType
    importance: Literal["high", "medium", "low"]
    importance_reason: str = Field(min_length=1, max_length=300)
    sentiment: Literal["positive", "negative", "neutral", "mixed"]
    fiscal_period: str | None = Field(default=None, max_length=40)
    figures: list[Figure] = Field(default_factory=list, max_length=15)
    near_duplicate_of: int | None = None
    one_line: str = Field(min_length=1, max_length=300)


class ClassifyReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    items: list[dict]          # each checked one by one as ItemLabel, so one bad item can't sink the batch


class Bullet(BaseModel):
    """One digest bullet (Milestone 6). Citations go in `cites`, not in the text."""
    model_config = ConfigDict(extra="forbid")
    text: str = Field(min_length=1, max_length=500)
    cites: list[str] = Field(min_length=1, max_length=8)
    fixes: int | None = None     # retry only: index of the failed bullet this one replaces


class DigestReply(BaseModel):
    model_config = ConfigDict(extra="forbid")
    bullets: list[dict] = Field(max_length=12)   # each checked one by one as Bullet


# ---------- checks Python runs on Claude's reply ----------

_FENCE = re.compile(r"^\s*```(?:json)?\s*|\s*```\s*$", re.I)


def extract_json(text: str) -> dict:
    """Claude sometimes wraps JSON in ```json fences or adds a word around it. Take the outer {...}."""
    text = _FENCE.sub("", text.strip())
    start, end = text.find("{"), text.rfind("}")
    if start == -1 or end <= start:
        raise ValueError("no JSON object in reply")
    return json.loads(text[start:end + 1])


def normalise(text: str) -> str:
    """For quote checks: lower case, unify quotes/dashes, collapse whitespace."""
    text = (text or "").lower()
    for a, b in (("’", "'"), ("‘", "'"), ("“", '"'), ("”", '"'), ("–", "-"), ("—", "-"),
                 (" ", " "), ("|", " ")):      # "|" = table cell separator in stored filings
        text = text.replace(a, b)
    return re.sub(r"\s+", " ", text).strip()


def _digits(value: str) -> str:
    return re.sub(r"[^0-9]", "", value)


def check_label(raw: dict, *, batch_ids: set[int], known_ids: set[int], watchlist: set[str],
                source_text: dict[int, str]) -> tuple[ItemLabel | None, str | None]:
    """Return (label, None) if the item's reply passes every check, else (None, reason)."""
    try:
        label = ItemLabel.model_validate(raw)
    except Exception as exc:  # pydantic.ValidationError
        return None, f"schema: {str(exc)[:400]}"
    if label.item_id not in batch_ids:
        return None, f"unknown item_id {label.item_id}"
    bad = [t for t in label.tickers if t not in watchlist]
    if bad:
        return None, f"tickers not on the watchlist: {bad}"
    if label.near_duplicate_of is not None and (label.near_duplicate_of == label.item_id
                                                or label.near_duplicate_of not in known_ids):
        return None, f"near_duplicate_of {label.near_duplicate_of} is not an item it was shown"
    text = normalise(source_text.get(label.item_id, ""))
    for f in label.figures:
        if normalise(f.quote) not in text:
            return None, f"figure quote not found in the item text: {f.quote[:120]!r}"
        if _digits(f.value) and _digits(f.value) not in _digits(f.quote):
            return None, f"figure value {f.value!r} is not in its quote {f.quote[:120]!r}"
    return label, None
