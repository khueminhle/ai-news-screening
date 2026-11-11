"""Shared test setup: every test uses tests/fixtures/watchlist.json, never the real watchlist."""
from pathlib import Path

import pytest

from connectors import base

TEST_WATCHLIST = Path(__file__).parent / "fixtures" / "watchlist.json"


@pytest.fixture(autouse=True)
def fixed_watchlist(monkeypatch):
    entries = base.load_watchlist_entries(TEST_WATCHLIST)
    import pipeline.run_classify as rc
    import pipeline.run_dashboard as rdash
    import pipeline.run_digest as rd
    for module in (rc, rd, rdash):
        monkeypatch.setattr(module, "load_watchlist_entries", lambda *a, **k: [dict(e) for e in entries])
    yield
