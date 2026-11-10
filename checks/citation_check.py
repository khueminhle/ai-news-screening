"""Citation check: every digest bullet cites at least one fact, and every cited ID exists in its fact sheet."""
from __future__ import annotations


def check_citations(cites: list[str], fact_ids: set[str]) -> str | None:
    """Return None if fine, else the reason."""
    if not cites:
        return "no citation"
    unknown = [c for c in cites if c not in fact_ids]
    if unknown:
        return f"cites IDs that are not in the fact sheet: {unknown}"
    return None
