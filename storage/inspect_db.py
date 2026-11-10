"""Look inside the database: structure, row counts, duplicate checks, latest runs.

    python -m storage.inspect_db               # everything
    python -m storage.inspect_db --schema      # tables and columns only
    python -m storage.inspect_db --duplicates  # duplicate checks only
"""
from __future__ import annotations

import argparse
import sqlite3
from pathlib import Path

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from storage.db import DB_PATH

# Each table's unique key: two rows with the same key would be a duplicate.
UNIQUE_KEYS = {
    "items": "source, source_id",
    "item_tickers": "item_id, ticker",
    "filings": "accession_no",
    "filing_files": "accession_no, file_name",
    "documents": "source, source_ref",
    "document_sections": "document_id, seq",
    "prices": "ticker, date, source",
    "macro_series": "series_id, date",
    "series_info": "series_id",
    "analyst_ratings": "ticker, period, source",
    "earnings_calendar": "ticker, fiscal_year, fiscal_quarter, source",
    "item_labels": "item_id, prompt_version",
    "financials": "ticker, taxonomy, concept, unit, period_start, period_end, accession_no",
}


def show_schema(conn: sqlite3.Connection) -> None:
    print("\n=== Structure (table: rows | columns) ===")
    tables = [r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]
    for t in tables:
        n = conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
        cols = conn.execute(f"PRAGMA table_info({t})").fetchall()
        key = UNIQUE_KEYS.get(t, "id")
        print(f"\n{t}  ({n:,} rows)   unique key: {key}")
        for c in cols:
            print(f"    {c[1]:<28} {c[2] or ''}")


def check_duplicates(conn: sqlite3.Connection) -> None:
    print("\n=== Duplicate check 1: same unique key twice (must all be 0) ===")
    for table, key in UNIQUE_KEYS.items():
        dup = conn.execute(f"SELECT COUNT(*) FROM (SELECT {key} FROM {table} GROUP BY {key} HAVING COUNT(*) > 1)").fetchone()[0]
        print(f"  {table:<20} {dup:>5}  {'OK' if dup == 0 else 'DUPLICATES FOUND'}")

    print("\n=== Duplicate check 2: possible repeats the unique key cannot catch (for information) ===")
    same_title = conn.execute("""SELECT source, clean_title, COUNT(*) n FROM items
                                 WHERE clean_title != '' AND item_type IN ('news', 'market_news')
                                 GROUP BY source, clean_title HAVING n > 1 ORDER BY n DESC""").fetchall()
    print(f"  same source + same headline, different story IDs: {len(same_title)}")
    for r in same_title[:10]:
        print(f"    {r['source']:<9} x{r['n']}  {r['clean_title'][:80]}")
    shared = conn.execute("""SELECT event_id, COUNT(*) n, GROUP_CONCAT(DISTINCT source) srcs FROM items
                             WHERE event_id IS NOT NULL GROUP BY event_id HAVING n > 1""").fetchall()
    print(f"  events with more than one copy (grouped, as intended): {len(shared)}")
    for r in shared[:10]:
        title = conn.execute("SELECT clean_title FROM events WHERE id = ?", (r["event_id"],)).fetchone()[0]
        print(f"    event {r['event_id']:<5} {r['n']} copies from {r['srcs']:<17} {title[:60]}")


def show_runs(conn: sqlite3.Connection) -> None:
    print("\n=== Latest runs ===")
    for r in conn.execute("""SELECT connector, run_day, started_at, status, counts FROM fetch_runs
                             ORDER BY id DESC LIMIT 12"""):
        print(f"  {r['started_at']}  {r['connector']:<9} {r['run_day']}  {r['status']:<9} {r['counts'][:90]}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Inspect data/signals.db")
    parser.add_argument("--schema", action="store_true", help="Tables and columns only")
    parser.add_argument("--duplicates", action="store_true", help="Duplicate checks only")
    args = parser.parse_args()

    conn = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)  # read-only: this script never changes data
    conn.row_factory = sqlite3.Row
    everything = not (args.schema or args.duplicates)
    if args.schema or everything:
        show_schema(conn)
    if args.duplicates or everything:
        check_duplicates(conn)
    if everything:
        show_runs(conn)
    conn.close()


if __name__ == "__main__":
    main()
