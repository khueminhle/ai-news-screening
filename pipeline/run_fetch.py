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


GAP_DAYS = 3                       # a ticker not covered for longer than this gets its filings refilled


def tickers_needing_backfill(conn, tickers: list[str], day: date) -> dict[str, int]:
    """{ticker: years to (re)load} for watchlist tickers whose SEC filings may be missing.

    - never seen: 2 years (unless it already has filings older than ~10 months from before this table existed)
    - last covered more than GAP_DAYS ago (removed and re-added, or runs were missed): 1 year if the gap is
      under a year, else 2 years. Reloading never duplicates: filings already stored are skipped.
    """
    need: dict[str, int] = {}
    for t in tickers:
        row = conn.execute("SELECT last_covered_day FROM sec_coverage WHERE ticker = ?", (t,)).fetchone()
        if row is None:
            cutoff = date.fromordinal(day.toordinal() - 300).isoformat()
            old = conn.execute("SELECT COUNT(*) FROM filings WHERE (',' || tickers || ',') LIKE ? AND filed_date <= ?",
                               (f"%,{t},%", cutoff)).fetchone()[0]
            if old:
                _set_coverage(conn, [t], day, history_years=AUTO_BACKFILL_YEARS, note="history loaded earlier")
            else:
                need[t] = AUTO_BACKFILL_YEARS
            continue
        last = row["last_covered_day"]
        gap = (day - date.fromisoformat(last)).days if last else 10_000
        if gap > GAP_DAYS:
            need[t] = 1 if gap <= 365 else AUTO_BACKFILL_YEARS
    return need


def _set_coverage(conn, tickers: list[str], day: date, *, history_years: int | None = None, note: str | None = None,
                  covered: bool = True) -> None:
    from connectors.base import utc_now_z
    for t in tickers:
        conn.execute("INSERT OR IGNORE INTO sec_coverage (ticker) VALUES (?)", (t,))
        if history_years is not None:
            conn.execute("UPDATE sec_coverage SET history_loaded_at = ?, history_years = ?, note = ? WHERE ticker = ?",
                         (utc_now_z(), history_years, note, t))
        if covered:
            conn.execute("UPDATE sec_coverage SET last_covered_day = ? WHERE ticker = ? "
                         "AND (last_covered_day IS NULL OR last_covered_day < ?)", (day.isoformat(), t, day.isoformat()))
    conn.commit()


def run(day: date, only: list[str] | None = None, backfill_sec_years: int | None = None) -> list[dict]:
    from connectors.base import load_watchlist
    conn = connect()
    results = []
    watch = load_watchlist()

    # SEC history: new tickers get 2 years; tickers not covered for > GAP_DAYS get the gap refilled.
    if (not only or "sec" in only) and not backfill_sec_years:
        need = tickers_needing_backfill(conn, watch, day)
        for years in sorted(set(need.values())):
            group = [t for t, y in need.items() if y == years]
            log.info("SEC history (%s year(s)) for %s", years, group)
            r = _run_connector(conn, "sec_backfill", *CONNECTORS["sec"], day,
                               {"tickers": group, "backfill_years": years}, raw_sub="backfill")
            r["counts"]["tickers"] = ",".join(group)
            r["counts"]["years"] = years
            results.append(r)
            if r["status"] != "failed":
                _set_coverage(conn, group, day, history_years=years, note=f"auto {years}y on {day.isoformat()}")

    for name, (module, cls) in CONNECTORS.items():
        if only and name not in only:
            continue
        kwargs = {"backfill_years": backfill_sec_years} if name == "sec" and backfill_sec_years else {}
        r = _run_connector(conn, name, module, cls, day, kwargs)
        results.append(r)
        if name == "sec" and r["status"] != "failed":
            # this run covered every watchlist ticker for today
            _set_coverage(conn, watch, day, history_years=backfill_sec_years,
                          note=f"manual {backfill_sec_years}y on {day.isoformat()}" if backfill_sec_years else None)

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
