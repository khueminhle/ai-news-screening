"""SQLite storage: open the database and save connector output into the clean tables.

    conn = connect()                       # data/signals.db, created on first use
    counts = save_items(conn, items)       # any connector's Items -> the right tables
    group_events(conn)                     # link copies of the same story

Update rules (agreed 2026-11-10):
  items, filings, documents      never duplicated; re-seeing an item only updates last_seen_at
  prices                         a bar is replaced only while is_final = 0, or if adj_close changed
  macro_series                   a revised FRED value replaces the old one, kept in revised_from
  analyst_ratings                updated if the month's counts change
  earnings_calendar              current + previous two values (with dates); every change logged
"""
from __future__ import annotations

import csv
import json
import sqlite3
from collections import Counter
from datetime import datetime, timedelta
from pathlib import Path

from connectors.base import PROJECT_ROOT, Item, clean_headline, to_utc_z, utc_now_z

DB_PATH = PROJECT_ROOT / "data" / "signals.db"
SCHEMA_PATH = Path(__file__).with_name("schema.sql")
PLACEHOLDERS = {"ONLY AVAILABLE IN PAID PLANS", "ONLY AVAILABLE IN CORPORATE PLANS"}
TIMING = {"bmo": "before open", "amc": "after close", "dmh": "during market hours"}
EVENT_WINDOW = timedelta(hours=48)


def connect(path: Path = DB_PATH) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.executescript(SCHEMA_PATH.read_text(encoding="utf-8"))
    return conn


def clean_text(value) -> str | None:
    """Empty strings and paid-plan placeholders become NULL."""
    if value is None:
        return None
    text = str(value).strip()
    return None if not text or text in PLACEHOLDERS else text


# ---------- runs ----------

def start_run(conn: sqlite3.Connection, connector: str, run_day: str) -> int:
    cur = conn.execute("INSERT INTO fetch_runs (connector, run_day, started_at) VALUES (?, ?, ?)",
                       (connector, run_day, utc_now_z()))
    conn.commit()
    return cur.lastrowid


def finish_run(conn: sqlite3.Connection, run_id: int, status: str, counts: dict, errors: list[str]) -> None:
    conn.execute("UPDATE fetch_runs SET finished_at = ?, status = ?, counts = ?, errors = ? WHERE id = ?",
                 (utc_now_z(), status, json.dumps(counts), json.dumps(errors), run_id))
    conn.commit()


# ---------- dispatcher ----------

def save_items(conn: sqlite3.Connection, items: list[Item]) -> dict:
    counts: Counter = Counter()
    for it in items:
        if it.item_type == "analyst_ratings":
            _save_ratings(conn, it, counts)
        elif it.item_type == "earnings_calendar":
            _save_earnings(conn, it, counts)
        elif it.item_type == "macro_series":
            _save_fred(conn, it, counts)
        elif it.item_type in ("price_daily", "benchmark_daily"):
            _save_prices(conn, it, counts)
        elif it.item_type == "company_facts":
            _save_facts(conn, it, counts)
        elif it.source == "sec_edgar":
            _save_filing(conn, it, counts)
        elif it.source == "federal_reserve":
            _save_fed(conn, it, counts)
        else:
            _save_news(conn, it, counts)
    conn.commit()
    return dict(counts)


# ---------- items (news, filings, Fed) ----------

def _upsert_item(conn, it: Item, counts: Counter, *, summary=None, publisher=None, sentiment=None,
                 document_id=None, extra: dict | None = None) -> int:
    now = utc_now_z()
    row = conn.execute("SELECT id FROM items WHERE source = ? AND source_id = ?", (it.source, it.source_id)).fetchone()
    if row:
        item_id = row["id"]
        conn.execute("UPDATE items SET last_seen_at = ?, document_id = COALESCE(document_id, ?) WHERE id = ?",
                     (now, document_id, item_id))
        counts["items_seen_again"] += 1
    else:
        cur = conn.execute(
            """INSERT INTO items (source, source_id, item_type, title, clean_title, summary, publisher, url,
                                  published_at, sentiment, document_id, extra, first_seen_at, last_seen_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (it.source, it.source_id, it.item_type, clean_text(it.title), clean_headline(it.title),
             clean_text(summary), clean_text(publisher), clean_text(it.url), to_utc_z(it.published_at),
             clean_text(sentiment), document_id, json.dumps(extra if extra is not None else it.extra, default=str),
             now, now))
        item_id = cur.lastrowid
        counts["items_added"] += 1
    for t in it.tickers:
        conn.execute("INSERT OR IGNORE INTO item_tickers (item_id, ticker) VALUES (?, ?)", (item_id, t))
    return item_id


def _save_news(conn, it: Item, counts: Counter) -> None:
    e = it.extra
    summary = e.get("summary") if it.source == "finnhub" else e.get("description")
    _upsert_item(conn, it, counts, summary=summary, publisher=e.get("publisher"), sentiment=e.get("sentiment"))


def _save_document(conn, counts: Counter, *, source: str, source_ref: str, doc_type: str, title: str,
                   published_at: str | None, blocks_path: str | None, local_path: str | None) -> int | None:
    row = conn.execute("SELECT id FROM documents WHERE source = ? AND source_ref = ?", (source, source_ref)).fetchone()
    if row:
        return row["id"]
    if not blocks_path or not Path(blocks_path).exists():
        return None
    parsed = json.loads(Path(blocks_path).read_text(encoding="utf-8"))
    blocks = parsed.get("blocks", [])
    full_text = "\n\n".join(b["content"] for b in blocks)
    found = parsed.get("sections_found")
    cur = conn.execute(
        """INSERT INTO documents (source, source_ref, doc_type, title, published_at, full_text, char_count,
                                  sections_found, warnings, local_path, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (source, source_ref, doc_type, title, to_utc_z(published_at), full_text, len(full_text),
         None if found is None else int(found), json.dumps(parsed.get("warnings", [])), local_path, utc_now_z()))
    doc_id = cur.lastrowid
    conn.executemany(
        "INSERT INTO document_sections (document_id, seq, section, heading, kind, content) VALUES (?, ?, ?, ?, ?, ?)",
        [(doc_id, i, b.get("section"), b.get("heading"), b["kind"], b["content"]) for i, b in enumerate(blocks)])
    counts["documents_added"] += 1
    counts["document_sections_added"] += len(blocks)
    if found is False:
        counts["documents_unsplit"] += 1
    return doc_id


def _save_fed(conn, it: Item, counts: Counter) -> None:
    e = it.extra
    doc_id = _save_document(conn, counts, source=it.source, source_ref=e.get("doc_id") or it.source_id,
                            doc_type=it.item_type, title=it.title, published_at=it.published_at,
                            blocks_path=e.get("blocks_path"), local_path=e.get("html_path"))
    description = e.get("rss_description")
    _upsert_item(conn, it, counts, summary=None if description == it.title else description,
                 publisher="Federal Reserve", document_id=doc_id)


def _save_filing(conn, it: Item, counts: Counter) -> None:
    e = it.extra
    accession = it.source_id
    main_doc_id = None
    doc_ids = {}
    for f in e.get("files", []):
        if not f.get("downloaded"):
            continue
        doc_id = _save_document(conn, counts, source=it.source, source_ref=f"{accession}/{f['file_name']}",
                                doc_type=f.get("doc_type"), title=f"{e.get('company_name')} {f.get('doc_type')}",
                                published_at=e.get("acceptance_datetime") or it.published_at,
                                blocks_path=f.get("blocks_path"), local_path=f.get("local_path"))
        doc_ids[f["file_name"]] = doc_id
        if f["file_name"] == e.get("primary_document"):
            main_doc_id = doc_id

    slim_extra = {k: v for k, v in e.items() if k != "files"}
    item_id = _upsert_item(conn, it, counts, publisher="SEC EDGAR", document_id=main_doc_id, extra=slim_extra)
    # published_at for a filing = the moment the SEC accepted it
    conn.execute("UPDATE items SET published_at = COALESCE(?, published_at) WHERE id = ?",
                 (to_utc_z(e.get("acceptance_datetime")), item_id))

    cur = conn.execute(
        """INSERT OR IGNORE INTO filings (accession_no, cik, tickers, company, form, filed_date, report_date,
                                          accepted_at, items_8k, index_url, item_id, created_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (accession, e.get("cik"), ",".join(it.tickers), e.get("company_name"), it.item_type, e.get("filed_date"),
         clean_text(e.get("report_date")), to_utc_z(e.get("acceptance_datetime")), clean_text(e.get("items")),
         e.get("index_url"), item_id, utc_now_z()))
    counts["filings_added"] += cur.rowcount

    for f in e.get("files", []):
        conn.execute(
            """INSERT INTO filing_files (accession_no, file_name, doc_type, description, size_bytes, downloaded,
                                         skip_reason, local_path, document_id)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (accession_no, file_name) DO UPDATE SET
                   downloaded = excluded.downloaded, skip_reason = excluded.skip_reason,
                   local_path = excluded.local_path, document_id = COALESCE(filing_files.document_id, excluded.document_id)
               WHERE filing_files.downloaded = 0""",
            (accession, f["file_name"], f.get("doc_type"), clean_text(f.get("description")), f.get("size_bytes"),
             int(bool(f.get("downloaded"))), f.get("skip_reason"), f.get("local_path"), doc_ids.get(f["file_name"])))
    counts["filing_files_recorded"] += len(e.get("files", []))


# ---------- prices ----------

def _save_prices(conn, it: Item, counts: Counter) -> None:
    e = it.extra
    raw_path = e.get("raw_path")
    if not raw_path or not Path(raw_path).exists():
        counts["prices_missing_raw"] += 1
        return
    ticker = it.tickers[0] if it.tickers else e["symbol"]
    last_date, partial = e.get("date"), bool(e.get("is_partial_session"))
    now = utc_now_z()
    before = conn.total_changes
    with open(raw_path, newline="", encoding="utf-8") as fh:
        for r in csv.DictReader(fh):
            if not r.get("Close") or r["Date"][:10] > last_date:
                continue
            day = r["Date"][:10]
            vals = [round(float(r[k]), 4) if r.get(k) not in (None, "") else None
                    for k in ("Open", "High", "Low", "Close", "Adj Close")]
            volume = int(float(r["Volume"])) if r.get("Volume") else None
            is_final = 0 if (partial and day == last_date) else 1
            conn.execute(
                """INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume, is_final, source, fetched_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, 'yfinance', ?)
                   ON CONFLICT (ticker, date, source) DO UPDATE SET
                       open = excluded.open, high = excluded.high, low = excluded.low, close = excluded.close,
                       adj_close = excluded.adj_close, volume = excluded.volume, is_final = excluded.is_final,
                       fetched_at = excluded.fetched_at
                   WHERE prices.is_final = 0
                      -- real dividend/split re-adjustments (>= ~0.07%) count; Yahoo's rounding jitter does not
                      OR abs(prices.adj_close - excluded.adj_close) > 0.0002 * abs(prices.adj_close)""",
                (ticker, day, *vals, volume, is_final, now))
    counts["price_rows_added_or_updated"] += conn.total_changes - before


# ---------- FRED ----------

def _save_fred(conn, it: Item, counts: Counter) -> None:
    e = it.extra
    conn.execute(
        """INSERT INTO series_info (series_id, label, grp, units, frequency, fred_title, last_updated)
           VALUES (?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (series_id) DO UPDATE SET label = excluded.label, grp = excluded.grp, units = excluded.units,
               frequency = excluded.frequency, fred_title = excluded.fred_title, last_updated = excluded.last_updated""",
        (e["series_id"], e.get("label"), e.get("group"), e.get("units"), e.get("frequency"), e.get("fred_title"),
         to_utc_z(e.get("fred_last_updated"))))
    raw_path = e.get("raw_path")
    if not raw_path or not Path(raw_path).exists():
        counts["macro_missing_raw"] += 1
        return
    now = utc_now_z()
    before_rev = conn.execute("SELECT COUNT(*) FROM macro_series WHERE revised_at IS NOT NULL").fetchone()[0]
    before = conn.total_changes
    for o in json.loads(Path(raw_path).read_text(encoding="utf-8")).get("observations", []):
        if o.get("value") in (".", "", None):
            continue
        conn.execute(
            """INSERT INTO macro_series (series_id, date, value, fetched_at) VALUES (?, ?, ?, ?)
               ON CONFLICT (series_id, date) DO UPDATE SET
                   revised_from = macro_series.value, revised_at = excluded.fetched_at,
                   value = excluded.value, fetched_at = excluded.fetched_at
               WHERE macro_series.value != excluded.value""",
            (e["series_id"], o["date"], float(o["value"]), now))
    counts["macro_rows_added_or_revised"] += conn.total_changes - before
    after_rev = conn.execute("SELECT COUNT(*) FROM macro_series WHERE revised_at IS NOT NULL").fetchone()[0]
    counts["macro_rows_revised"] += after_rev - before_rev


# ---------- SEC company facts ----------

def _save_facts(conn, it: Item, counts: Counter) -> None:
    """Every fact exactly as SEC reports it. A fact already stored (same period + filing) is left alone."""
    from connectors.sec_facts import iter_facts
    raw_path = it.extra.get("raw_path")
    if not raw_path or not Path(raw_path).exists():
        counts["facts_missing_raw"] += 1
        return
    data = json.loads(Path(raw_path).read_text(encoding="utf-8"))
    now = utc_now_z()
    before = conn.total_changes
    for ticker in it.tickers:
        conn.executemany(
            """INSERT OR IGNORE INTO financials (ticker, cik, taxonomy, concept, label, unit, period_start, period_end,
                   value, fiscal_year, fiscal_period, form, accession_no, filed_date, frame, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            [(ticker, it.extra["cik"], f["taxonomy"], f["concept"], f["label"], f["unit"], f["period_start"],
              f["period_end"], f["value"], f["fiscal_year"], f["fiscal_period"], f["form"], f["accession_no"],
              f["filed_date"], f["frame"], now) for f in iter_facts(data)])
    counts["financial_facts_added"] += conn.total_changes - before


# ---------- analyst ratings ----------

def _save_ratings(conn, it: Item, counts: Counter) -> None:
    e = it.extra
    raw_path = e.get("raw_path")
    rows = json.loads(Path(raw_path).read_text(encoding="utf-8")) if raw_path and Path(raw_path).exists() else [
        {"period": e["period"], **{k: v for k, v in e["counts"].items()}}]
    now = utc_now_z()
    before = conn.total_changes
    for r in rows:
        if not r.get("period"):
            continue
        nums = [r.get("strongBuy"), r.get("buy"), r.get("hold"), r.get("sell"), r.get("strongSell")]
        conn.execute(
            """INSERT INTO analyst_ratings (ticker, period, strong_buy, buy, hold, sell, strong_sell, total, source, fetched_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT (ticker, period, source) DO UPDATE SET
                   strong_buy = excluded.strong_buy, buy = excluded.buy, hold = excluded.hold, sell = excluded.sell,
                   strong_sell = excluded.strong_sell, total = excluded.total, fetched_at = excluded.fetched_at
               WHERE (analyst_ratings.strong_buy, analyst_ratings.buy, analyst_ratings.hold, analyst_ratings.sell,
                      analyst_ratings.strong_sell) IS NOT (excluded.strong_buy, excluded.buy, excluded.hold,
                      excluded.sell, excluded.strong_sell)""",
            (it.tickers[0], r["period"], *nums, sum(n or 0 for n in nums), it.source, now))
    counts["rating_rows_added_or_updated"] += conn.total_changes - before


# ---------- earnings calendar ----------

TRACKED = ("report_date", "eps_estimate", "revenue_estimate")


def _save_earnings(conn, it: Item, counts: Counter) -> None:
    e = it.extra
    ticker, year, quarter = it.tickers[0], e.get("year"), e.get("quarter")
    if year is None or quarter is None:
        counts["earnings_skipped_no_quarter"] += 1
        return
    now, today = utc_now_z(), utc_now_z()[:10]
    new = {"report_date": it.published_at or None, "eps_estimate": e.get("eps_estimate"),
           "revenue_estimate": e.get("revenue_estimate")}
    timing = TIMING.get((e.get("hour") or "").lower())
    key = (ticker, year, quarter, it.source)
    row = conn.execute("""SELECT * FROM earnings_calendar
                          WHERE ticker = ? AND fiscal_year = ? AND fiscal_quarter = ? AND source = ?""", key).fetchone()
    if row is None:
        conn.execute(
            """INSERT INTO earnings_calendar (ticker, fiscal_year, fiscal_quarter, report_date, report_date_seen,
                   timing, eps_estimate, eps_estimate_seen, revenue_estimate, revenue_estimate_seen,
                   eps_actual, revenue_actual, source, first_seen_at, updated_at)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (ticker, year, quarter, new["report_date"], today, timing, new["eps_estimate"], today,
             new["revenue_estimate"], today, e.get("eps_actual"), e.get("revenue_actual"), it.source, now, now))
        counts["earnings_added"] += 1
        return

    sets, params, changed = [], [], False
    for f in TRACKED:
        if new[f] is None or row[f] == new[f]:
            continue
        # shift: prev1 -> prev2, current -> prev1, new -> current
        sets += [f"{f}_prev2 = ?", f"{f}_prev2_seen = ?", f"{f}_prev1 = ?", f"{f}_prev1_seen = ?", f"{f} = ?", f"{f}_seen = ?"]
        params += [row[f"{f}_prev1"], row[f"{f}_prev1_seen"], row[f], row[f"{f}_seen"], new[f], today]
        _log_change(conn, key, f, row[f], new[f], now)
        changed = True
    for f, v in (("timing", timing), ("eps_actual", e.get("eps_actual")), ("revenue_actual", e.get("revenue_actual"))):
        if v is not None and row[f] != v:
            sets.append(f"{f} = ?")
            params.append(v)
            _log_change(conn, key, f, row[f], v, now)
            changed = True
    if changed:
        conn.execute(f"""UPDATE earnings_calendar SET {', '.join(sets)}, updated_at = ?
                         WHERE ticker = ? AND fiscal_year = ? AND fiscal_quarter = ? AND source = ?""",
                     (*params, now, *key))
        counts["earnings_updated"] += 1


def _log_change(conn, key, field, old, new, now) -> None:
    conn.execute("""INSERT INTO earnings_changes (ticker, fiscal_year, fiscal_quarter, field, old_value, new_value, changed_at)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (key[0], key[1], key[2], field,
                                                       None if old is None else str(old), str(new), now))


# ---------- events: copies of the same story ----------

def group_events(conn: sqlite3.Connection) -> dict:
    """Same story = shares a ticker + same cleaned headline + published within 48 hours."""
    counts: Counter = Counter()
    pending = conn.execute("""SELECT id, clean_title, published_at FROM items
                              WHERE event_id IS NULL AND item_type = 'news' AND clean_title != ''
                              ORDER BY published_at""").fetchall()
    for it in pending:
        tickers = {r["ticker"] for r in conn.execute("SELECT ticker FROM item_tickers WHERE item_id = ?", (it["id"],))}
        published = _parse(it["published_at"])
        match = None
        for ev in conn.execute("SELECT * FROM events WHERE clean_title = ?", (it["clean_title"],)):
            ev_tickers = set(filter(None, (ev["tickers"] or "").split(",")))
            first = _parse(ev["first_published_at"])
            if tickers & ev_tickers and published and first and abs(published - first) <= EVENT_WINDOW:
                match = ev
                break
        if match:
            conn.execute("UPDATE items SET event_id = ? WHERE id = ?", (match["id"], it["id"]))
            merged = ",".join(sorted(set(filter(None, (match["tickers"] or "").split(","))) | tickers))
            conn.execute("UPDATE events SET tickers = ? WHERE id = ?", (merged, match["id"]))
            counts["items_linked_to_existing_event"] += 1
        else:
            cur = conn.execute("""INSERT INTO events (clean_title, tickers, first_published_at, primary_item_id, created_at)
                                  VALUES (?, ?, ?, ?, ?)""",
                               (it["clean_title"], ",".join(sorted(tickers)), it["published_at"], it["id"], utc_now_z()))
            conn.execute("UPDATE items SET event_id = ? WHERE id = ?", (cur.lastrowid, it["id"]))
            counts["events_created"] += 1
    conn.commit()
    return dict(counts)


def _parse(value: str | None) -> datetime | None:
    if not value or len(value) < 11:
        return None
    return datetime.fromisoformat(value.replace("Z", "+00:00"))
