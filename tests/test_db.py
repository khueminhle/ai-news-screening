"""Database rules: no duplicates on re-runs, and the agreed update rules per table."""
import json
import shutil
from pathlib import Path

import pytest

from connectors.base import Item
from storage.db import connect, group_events, save_items

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def conn(tmp_path):
    c = connect(tmp_path / "test.db")
    yield c
    c.close()


def news(source="finnhub", sid="1", title="Ubiquiti Soars 15% on Record Quarter", tickers=("UI",),
         published="2026-11-06T12:15:01+00:00", **extra):
    return Item(source=source, source_id=sid, tickers=list(tickers), title=title, url="https://x",
                published_at=published, item_type="news", extra=extra)


def count(conn, table):
    return conn.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_rerun_creates_no_duplicates(conn):
    items = [news(sid="1"), news(sid="2", title="Another story")]
    first = save_items(conn, items)
    second = save_items(conn, items)
    assert first["items_added"] == 2 and "items_added" not in second
    assert second["items_seen_again"] == 2
    assert count(conn, "items") == 2 and count(conn, "item_tickers") == 2


def test_placeholders_and_empty_strings_become_null(conn):
    save_items(conn, [news(source="newsdata", description="ONLY AVAILABLE IN PAID PLANS"),
                      news(source="finnhub", sid="9", summary="")])
    assert [r[0] for r in conn.execute("SELECT summary FROM items")] == [None, None]


def test_times_are_stored_in_one_utc_format(conn):
    save_items(conn, [news(sid="a", published="2026-11-08 17:30:00"), news(sid="b", published=1793984525)])
    times = sorted(r[0] for r in conn.execute("SELECT published_at FROM items"))
    assert times == ["2026-11-06T17:02:05Z", "2026-11-08T17:30:00Z"]


def test_events_same_ticker_same_headline_within_48h(conn):
    save_items(conn, [
        news(source="finnhub", sid="1", title="Ubiquiti Soars 15% on Record Quarter"),
        news(source="newsdata", sid="2", title="Ubiquiti soars 15% on record quarter - Yahoo Finance",
             published="2026-11-07T10:00:00Z"),                                  # same story, 22h later
        news(source="newsdata", sid="3", title="Ubiquiti Soars 15% on Record Quarter",
             published="2026-11-10T12:00:00Z"),                                  # 4 days later: new event
        news(source="newsdata", sid="4", title="Ubiquiti Soars 15% on Record Quarter", tickers=("AAPL",)),
    ])
    group_events(conn)
    ev = dict(conn.execute("SELECT source_id, event_id FROM items").fetchall())
    assert ev["1"] == ev["2"]
    assert ev["3"] != ev["1"] and ev["4"] != ev["1"]


def price_item(tmp_path, partial: bool):
    csv = tmp_path / "UI.csv"
    shutil.copy(FIXTURES / "yfinance_UI.csv", csv)
    last = csv.read_text().strip().splitlines()[-1].split(",")[0][:10]
    return Item(source="yfinance", source_id=f"UI:{last}", tickers=["UI"], title="", url="", published_at=last,
                item_type="price_daily", extra={"raw_path": str(csv), "date": last, "is_partial_session": partial,
                                                "symbol": "UI"}), csv, last


def test_partial_price_bar_is_replaced_later(conn, tmp_path):
    item, csv, last = price_item(tmp_path, partial=True)
    save_items(conn, [item])
    assert conn.execute("SELECT is_final FROM prices WHERE date = ?", (last,)).fetchone()[0] == 0
    rows = csv.read_text().strip().splitlines()
    cells = rows[-1].split(",")
    cells[4] = "700.0"  # final close differs from the intraday one
    csv.write_text("\n".join(rows[:-1] + [",".join(cells)]) + "\n")
    item.extra["is_partial_session"] = False
    save_items(conn, [item])
    final = conn.execute("SELECT close, is_final FROM prices WHERE date = ?", (last,)).fetchone()
    assert tuple(final) == (700.0, 1)
    assert count(conn, "prices") == 4


def test_fred_revision_keeps_old_value(conn, tmp_path):
    raw = tmp_path / "DGS10.json"
    shutil.copy(FIXTURES / "fred_DGS10.json", raw)
    it = Item(source="fred", source_id="DGS10:x", tickers=[], title="", url="", published_at="2026-11-05",
              item_type="macro_series", extra={"raw_path": str(raw), "series_id": "DGS10", "label": "10y",
                                               "group": "rates", "units": "Percent"})
    save_items(conn, [it])
    n = count(conn, "macro_series")
    data = json.loads(raw.read_text())
    revised_date, old = data["observations"][-1]["date"], float(data["observations"][-1]["value"])
    data["observations"][-1]["value"] = "4.99"
    raw.write_text(json.dumps(data))
    save_items(conn, [it])
    row = conn.execute("SELECT value, revised_from FROM macro_series WHERE series_id='DGS10' AND date=?",
                       (revised_date,)).fetchone()
    assert tuple(row) == (4.99, old) and count(conn, "macro_series") == n


def earnings(eps, date="2027-02-01", rev=17.1e9):
    return Item(source="finnhub", source_id="earnings:AMD", tickers=["AMD"], title="", url="", published_at=date,
                item_type="earnings_calendar", extra={"year": 2026, "quarter": 4, "hour": "amc",
                                                      "eps_estimate": eps, "revenue_estimate": rev})


def test_earnings_keep_previous_two_estimates_and_log(conn):
    for eps in (2.90, 2.95, 3.00, 3.05):
        save_items(conn, [earnings(eps)])
    row = conn.execute("SELECT * FROM earnings_calendar").fetchone()
    assert (row["eps_estimate"], row["eps_estimate_prev1"], row["eps_estimate_prev2"]) == (3.05, 3.00, 2.95)
    assert row["timing"] == "after close"
    assert count(conn, "earnings_changes") == 3          # every change kept, beyond the last two
    save_items(conn, [earnings(3.05)])                    # unchanged: nothing logged
    assert count(conn, "earnings_changes") == 3


def test_ratings_history_saved_from_raw(conn, tmp_path):
    raw = tmp_path / "PLTR.json"
    shutil.copy(FIXTURES / "finnhub_recommendations_PLTR.json", raw)
    it = Item(source="finnhub", source_id="recommendation:PLTR", tickers=["PLTR"], title="", url="",
              published_at="2026-11-01", item_type="analyst_ratings", extra={"raw_path": str(raw)})
    save_items(conn, [it])
    save_items(conn, [it])
    assert count(conn, "analyst_ratings") == 3
    total = conn.execute("SELECT total FROM analyst_ratings WHERE period='2026-11-01'").fetchone()[0]
    assert total == 41


# ---------- automatic SEC history: new tickers and coverage gaps ----------

def test_new_tickers_and_gaps_need_backfill(conn):
    from datetime import date
    from pipeline.run_fetch import _set_coverage, tickers_needing_backfill
    conn.execute("""INSERT INTO filings (accession_no, cik, tickers, form, filed_date, created_at)
                    VALUES ('old-1', 1, 'AAPL', '10-K', '2025-11-01', 'x')""")
    conn.commit()
    day = date(2026, 11, 12)
    assert tickers_needing_backfill(conn, ["AAPL", "CI", "GRAB"], day) == {"CI": 2, "GRAB": 2}   # AAPL loaded earlier
    _set_coverage(conn, ["CI", "GRAB"], day, history_years=2, note="auto")
    assert tickers_needing_backfill(conn, ["AAPL", "CI", "GRAB"], date(2026, 11, 13)) == {}       # covered yesterday
    # runs missed for a week -> the gap is refilled (1 year is enough)
    assert tickers_needing_backfill(conn, ["CI"], date(2026, 11, 20)) == {"CI": 1}


def test_removed_for_a_year_then_added_back_is_refilled(conn):
    from datetime import date
    from pipeline.run_fetch import _set_coverage, tickers_needing_backfill
    _set_coverage(conn, ["CI"], date(2026, 11, 12), history_years=2, note="auto")      # covered 2024-2026
    # removed from the watchlist: its last covered day stops moving; re-added in 2028
    assert tickers_needing_backfill(conn, ["CI"], date(2028, 1, 15)) == {"CI": 2}       # 2027 refilled


def test_run_backfills_only_tickers_that_need_it(tmp_path, monkeypatch):
    from datetime import date
    from connectors import base
    from pipeline import run_fetch
    path = tmp_path / "f.db"
    monkeypatch.setattr(run_fetch, "connect", lambda: connect(path))
    monkeypatch.setattr(base, "load_watchlist", lambda *a, **k: ["AAPL", "CI"])
    calls = []

    def fake(conn, name, module, cls, day, kwargs, raw_sub=""):
        calls.append((name, kwargs))
        return {"connector": name, "status": "ok", "counts": {}, "errors": [], "seconds": 0}
    monkeypatch.setattr(run_fetch, "_run_connector", fake)
    run_fetch.run(date(2026, 11, 12), only=["sec"])
    assert calls[0] == ("sec_backfill", {"tickers": ["AAPL", "CI"], "backfill_years": 2}) and calls[1][0] == "sec"
    calls.clear()
    run_fetch.run(date(2026, 11, 13), only=["sec"])                   # next day: covered, nothing extra
    assert [c[0] for c in calls] == ["sec"]
