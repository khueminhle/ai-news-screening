"""Fact check (check 1): every number Claude writes must appear in the text of a fact the bullet cites.

It catches invented numbers and numbers taken from a source the bullet does not cite. It does not judge
whether the source itself is right (that "check 2" was dropped on 2026-11-10 by decision).

Numbers are compared by their digits and decimal point: "$1,026.2 million" matches "1,026.2";
"+16.8%" matches "16.8". Dates, years, quarter names, form names (10-Q, 8-K), index names
(S&P 500, Nasdaq-100) and maturities (10-year) are not treated as numbers.
"""
from __future__ import annotations

import re

MONTHS = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
NOT_NUMBERS = re.compile("|".join([
    r"\[[^\]]*\]",                                  # citation markers, e.g. [N82]
    r"\b\d{4}-\d{2}-\d{2}\b",                       # ISO dates
    rf"\b{MONTHS}\s+\d{{1,2}}(?:st|nd|rd|th)?\b",   # Nov 6, November 6th
    rf"\b\d{{1,2}}\s+{MONTHS}\b",                   # 6 Nov
    r"\bS&P\s?500\b", r"\bNasdaq[- ]?100\b", r"\bRussell\s?\d{4}\b", r"\bDow\s?30\b",
    r"\b\d+-[KQ](?:/A)?\b",                         # 10-K, 8-K, 10-Q/A
    r"\b\d{1,2}-(?:year|month|week|day)s?\b",       # 10-year
    r"\b(?:Q|H)[1-4]\b", r"\bFY\s?\d{2,4}\b", r"\bF?Q[1-4]\s?FY\d{2}\b",
    r"\b[A-Z]{1,5}\d+[A-Za-z]*\b",                  # product / model names: EPYC9005, TPU8tx, M5
    r"\b(?:19|20)\d{2}\b",                          # years
]), re.I)
NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")


def _core(token: str) -> str:
    """'1,026.20' -> '1026.2'; '05' -> '5'."""
    token = token.replace(",", "")
    if "." in token:
        token = token.rstrip("0").rstrip(".")
    return token.lstrip("0") or "0"


def numbers_in(text: str) -> list[str]:
    cleaned = NOT_NUMBERS.sub(" ", text or "")
    return [_core(m.group(0)) for m in NUMBER.finditer(cleaned)]


def check_numbers(bullet_text: str, cited_texts: list[str]) -> str | None:
    """Return None if every number in the bullet is in a cited fact, else the reason."""
    available: set[str] = set()
    for t in cited_texts:
        available.update(_core(m.group(0)) for m in NUMBER.finditer(t or ""))
    missing = [n for n in numbers_in(bullet_text) if n not in available]
    if missing:
        return f"number(s) not in the cited facts: {sorted(set(missing))}"
    return None
