"""Run every connector, save everything to the database, and log each run.

    python -m pipeline.run_fetch                         # all connectors, today
    python -m pipeline.run_fetch --only sec fred         # some connectors
    python -m pipeline.run_fetch --backfill-sec-years 2  # one-off: SEC filings from the last 2 years
    python -m pipeline.run_fetch --day 2026-11-06

"Today" is the date at the time of the fetch. A connector that fails is logged as
'failed' in fetch_runs and the others still run. Each connector also writes its
data/raw/<source>/<day>/items.json as before.
"""
from __future__ import annotations

import argparse
import importlib
import logging
import os
import time
import traceback
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import write_items_json
from storage.db import DB_PATH, connect, finish_run, group_events, save_items, start_run

log = logging.getLogger("pipeline")

# name -> (module, class). Imported one by one so a broken connector can't stop the others.
CONNECTORS = {
    "sec": ("connectors.sec", "SecEdgarConnector"),
    "sec_facts": ("connectors.sec_facts", "SecFactsConnector"),
    "finnhub": ("connectors.finnhub", "FinnhubConnector"),
    "newsdata": ("connectors.newsdata", "NewsDataConnector"),
    "fed": ("connectors.fed", "FedConnector"),
    "fred": ("connectors.macro_fred", "FredConnector"),
    "yfinance": ("connectors.prices_yfinance", "YFinancePriceConnector"),
}


AUTO_BACKFILL_YEARS = 2


def _run_connector(conn, name: str, module: str, cls: str, day: date, kwargs: dict, raw_sub: str = "") -> dict:
    started = time.monotonic()
    run_id = start_run(conn, name, day.isoformat())
    errors: list[str] = []
    counts: dict = {}
    status = "failed"
    try:
        connector = getattr(importlib.import_module(module), cls)(**kwargs)
        items = connector.fetch(day)
        write_items_json(connector.raw_dir / raw_sub if raw_sub else connector.raw_dir, day, items)
        counts = {"items_fetched": len(items), **save_items(conn, items)}
        errors = list(connector.errors)
        status = "degraded" if errors else "ok"
    except Exception as exc:
        errors.append(f"{type(exc).__name__}: {exc}")
        log.error("%s failed:\n%s", name, traceback.format_exc())
    finish_run(conn, run_id, status, counts, errors)
    return {"connector": name, "status": status, "counts": counts, "errors": errors,
            "seconds": round(time.monotonic() - started, 1)}


def tickers_needing_backfill(conn, tickers: list[str], day: date) -> list[str]:
    """Watchlist tickers whose 2-year SEC history was never loaded.

    A ticker counts as done once it is in sec_backfills. Tickers that already have filings older than ~10 months
    (loaded before this list existed) are recorded as done instead of being loaded again.
    """
    cutoff = date.fromordinal(day.toordinal() - 300).isoformat()
    need = []
    for t in tickers:
        if conn.execute("SELECT 1 FROM sec_backfills WHERE ticker = ?", (t,)).fetchone():
            continue
        old = conn.execute("""SELECT COUNT(*) FROM filings WHERE (',' || tickers || ',') LIKE ? AND filed_date <= ?""",
                           (f"%,{t},%", cutoff)).fetchone()[0]
        if old:
            mark_backfilled(conn, [t], "already loaded earlier")
        else:
            need.append(t)
    return need


def mark_backfilled(conn, tickers: list[str], note: str) -> None:
    from connectors.base import utc_now_z
    conn.executemany("INSERT OR REPLACE INTO sec_backfills (ticker, years, done_at, note) VALUES (?, ?, ?, ?)",
                     [(t, AUTO_BACKFILL_YEARS, utc_now_z(), note) for t in tickers])
    conn.commit()


def run(day: date, only: list[str] | None = None, backfill_sec_years: int | None = None) -> list[dict]:
    from connectors.base import load_watchlist
    conn = connect()
    results = []
    watch = load_watchlist()

    # New tickers on the watchlist get their SEC history loaded once, automatically (only those tickers).
    if (not only or "sec" in only) and not backfill_sec_years:
        new = tickers_needing_backfill(conn, watch, day)
        if new:
            log.info("SEC history for new watchlist tickers: %s", new)
            r = _run_connector(conn, "sec_backfill", *CONNECTORS["sec"], day,
                               {"tickers": new, "backfill_years": AUTO_BACKFILL_YEARS}, raw_sub="backfill")
            r["counts"]["tickers"] = ",".join(new)
            results.append(r)
            if r["status"] != "failed":
                mark_backfilled(conn, new, f"auto backfill {day.isoformat()}")

    for name, (module, cls) in CONNECTORS.items():
        if only and name not in only:
            continue
        kwargs = {"backfill_years": backfill_sec_years} if name == "sec" and backfill_sec_years else {}
        r = _run_connector(conn, name, module, cls, day, kwargs)
        results.append(r)
        if name == "sec" and backfill_sec_years and r["status"] != "failed":
            mark_backfilled(conn, watch, f"manual backfill {day.isoformat()}")

    events = group_events(conn)
    results.append({"connector": "events", "status": "ok", "counts": events, "errors": [], "seconds": 0})
    conn.close()
    return results


def main() -> None:
    parser = argparse.ArgumentParser(description="Run all connectors and save to the database")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD or 'today' (default: the date at the time of fetch)")
    parser.add_argument("--only", nargs="*", choices=list(CONNECTORS), help="Run only these connectors")
    parser.add_argument("--backfill-sec-years", type=int, help="One-off: SEC filings from the last N years")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "WARNING"), format="%(asctime)s %(levelname)s %(name)s %(message)s")
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)
    results = run(day, args.only, args.backfill_sec_years)

    print(f"\nRun for {day}  ->  {DB_PATH}\n")
    print(f"  {'connector':<10} {'status':<9} {'secs':>6}  saved")
    for r in results:
        counts = ", ".join(f"{k}={v}" for k, v in r["counts"].items() if v) or "-"
        print(f"  {r['connector']:<10} {r['status']:<9} {r['seconds']:>6}  {counts}")
    for r in results:
        if r["errors"]:
            print(f"\n  {r['connector']}: {len(r['errors'])} warning(s)/error(s)")
            for e in r["errors"][:10]:
                print("    - " + e[:200])
            if len(r["errors"]) > 10:
                print(f"    ... {len(r['errors']) - 10} more in fetch_runs")


if __name__ == "__main__":
    main()
