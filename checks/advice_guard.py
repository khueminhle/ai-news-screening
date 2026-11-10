"""Advice guard: the digest never gives buy / sell / hold advice in its own voice.

Advice words are allowed only when the bullet attributes them to someone (an analyst, a bank, a publisher,
Finnhub's rating counts) AND cites a news item or the analyst counts.
"""
from __future__ import annotations

import re

ADVICE = re.compile(r"""\b(
      strong\s+buy | buy(?![-\s]?(?:back|side)) | sell(?![-\s]?(?:off|side))
    | (?:rated?|rating\s+of|a|as\s+a)\s+hold | (?:upgrade[sd]?|downgrade[sd]?)\b[^.]{0,30}\bto\s+hold
                                                   # "hold" only as a rating: "the Fed decided to hold rates" is fine
    | overweight | underweight | outperform | underperform | accumulate | reduce\s+exposure
    | price\s+target | target\s+price | upside\s+to       # "fair value" is an accounting term in filings: not advice
    | (?:we|you|investors)\s+should | should\s+(?:buy|sell|own|consider|avoid|add|trim)
    | recommend(?:s|ed|ation)? | undervalued | overvalued | attractive\s+entry | worth\s+buying
)\b""", re.I | re.X)

ATTRIBUTION = re.compile(r"""\b(
      according\s+to | said | says | reported | reports | per | wrote | noted
    | analysts? | rated | rating | ratings | counts | upgrade[sd]? | downgrade[sd]?
    | initiated | reiterated | raised | lowered | cut | set | maintained
)\b""", re.I | re.X)


def check_advice(text: str, cited_types: set[str]) -> str | None:
    """Return None if fine, else the reason."""
    found = sorted({m.group(0).lower() for m in ADVICE.finditer(text)})
    if not found:
        return None
    attributed = ATTRIBUTION.search(text) and cited_types & {"news", "market_news", "analyst_counts"}
    if attributed:
        return None
    return f"advice language in the system's own voice: {found}"
