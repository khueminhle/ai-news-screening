"""Milestone 5: Claude labels every new item (news, market news, Fed releases, new SEC filings).

    python -m pipeline.run_classify                  # all unlabelled items
    python -m pipeline.run_classify --limit 10       # try one batch first
    python -m pipeline.run_classify --dry-run        # build the input files only, no Claude

What happens:
  1. Python picks the items with no label yet (the 2-year SEC backfill is skipped).
  2. Groups them by ticker, BATCH_SIZE per batch, and writes
        data/llm/<day>/classify/<run>/<TICKER>_batchNN_input.json
  3. Prompt = rules (llm/prompts/classify.md) + that input. Claude has no tools: the prompt is all it sees.
  4. Up to MAX_PARALLEL Claude processes at once. Each reply comes back through the pipe and
     Python writes it to  .../<TICKER>_batchNN_output.json
  5. Python checks every item's labels (schema, ticker list, quotes really in the text).
     Pass -> item_labels / item_figures in signals.db.  Fail -> retried once -> llm_quarantine.
"""
from __future__ import annotations

import argparse
import asyncio
import html
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import PROJECT_ROOT, load_watchlist_entries, utc_now_z
from llm import claude_client
from llm.schemas import EVENT_TYPES, check_label, extract_json
from storage.db import connect

PROMPT_VERSION = "classify-v1"          # bump when llm/prompts/classify.md changes
PROMPT_PATH = PROJECT_ROOT / "llm" / "prompts" / "classify.md"
LLM_DIR = PROJECT_ROOT / "data" / "llm"
BATCH_SIZE = 10
MAX_PARALLEL = 10
MARKET = "MARKET"                       # batch name for items with no watchlist ticker (macro, Fed, market news)
TEXT_CHARS = 3000                       # how much of a filing / Fed text Claude sees
RECENT_WINDOW = timedelta(hours=48)
RECENT_MAX = 20


# ---------- 1. which items ----------

def pending_items(conn, *, limit: int | None = None, ticker: str | None = None,
                  retry_quarantine: bool = False) -> list[dict]:
    quarantine_rule = "" if retry_quarantine else """
          AND NOT EXISTS (SELECT 1 FROM llm_quarantine q
                          WHERE q.item_id = i.id AND q.prompt_version = :v AND q.resolved = 0)"""
    rows = conn.execute(f"""
        SELECT i.* FROM items i
        WHERE NOT EXISTS (SELECT 1 FROM item_labels l WHERE l.item_id = i.id AND l.prompt_version = :v)
          {quarantine_rule}
        ORDER BY i.published_at, i.id""", {"v": PROMPT_VERSION}).fetchall()
    out = []
    for r in rows:
        extra = json.loads(r["extra"] or "{}")
        if r["source"] == "sec_edgar" and extra.get("is_backfill"):
            continue                    # decision: the 2-year backfill is not classified
        item = dict(r)
        item["extra"] = extra
        item["tickers"] = [t["ticker"] for t in conn.execute(
            "SELECT ticker FROM item_tickers WHERE item_id = ? ORDER BY ticker", (r["id"],))]
        if ticker and ticker not in item["tickers"]:
            continue
        out.append(item)
    return out[:limit] if limit else out


# ---------- 2. what Claude sees for each item ----------

def _doc_text(conn, document_id: int | None, limit: int) -> str:
    if not document_id:
        return ""
    row = conn.execute("SELECT full_text FROM documents WHERE id = ?", (document_id,)).fetchone()
    return (row["full_text"] or "")[:limit] if row else ""


def _section_text(conn, document_id: int | None, section: str, limit: int) -> str:
    if not document_id:
        return ""
    parts = [r["content"] for r in conn.execute(
        "SELECT content FROM document_sections WHERE document_id = ? AND section = ? ORDER BY seq",
        (document_id, section))]
    return "\n".join(parts)[:limit]


def _filing_text(conn, item: dict) -> str:
    form, doc_id, accession = item["item_type"], item["document_id"], item["source_id"]
    if form.startswith("8-K"):
        press = conn.execute("""SELECT id FROM documents WHERE source = 'sec_edgar' AND source_ref LIKE ?
                                AND doc_type LIKE 'EX-99%' ORDER BY doc_type LIMIT 1""", (f"{accession}/%",)).fetchone()
        # the 8-K's own Item sections (skip the cover page boilerplate), then the press release
        main = "\n".join(r["content"] for r in conn.execute(
            "SELECT content FROM document_sections WHERE document_id = ? AND section != 'Cover' ORDER BY seq",
            (doc_id,)))[:1500] if doc_id else ""
        return main + ("\n\n[Press release / exhibit]\n" + _doc_text(conn, press["id"], TEXT_CHARS - 500) if press else "")
    section = "Part I Item 2" if form.startswith("10-Q") else "Item 7"   # MD&A: the management discussion
    return _section_text(conn, doc_id, section, TEXT_CHARS) or _doc_text(conn, doc_id, TEXT_CHARS)


def build_entry(conn, item: dict) -> tuple[dict, str]:
    """Return (what goes in the prompt, the text that figure quotes are checked against)."""
    title = item["title"] or ""
    summary = html.unescape(item["summary"] or "")
    if summary == title:
        summary = ""
    entry = {"item_id": item["id"], "title": title, "published_at": item["published_at"],
             "tickers_tagged": item["tickers"]}
    text = ""
    if item["source"] == "sec_edgar":
        e = item["extra"]
        entry.update(kind="sec_filing", form=item["item_type"], company=e.get("company_name"),
                     period=e.get("report_date") or None, items_8k=e.get("items") or None)
        text = _filing_text(conn, item)
    elif item["source"] == "federal_reserve":
        entry.update(kind="fed_release", fed_type=item["item_type"])
        text = _doc_text(conn, item["document_id"], TEXT_CHARS)
    else:
        entry.update(kind=item["item_type"], publisher=item["publisher"])
    if summary:
        entry["summary"] = summary
    if text:
        entry["text"] = text
    return entry, "\n".join(x for x in (title, summary, text) if x)


# ---------- 3. batches ----------

def group_key(item: dict, order: dict[str, int]) -> str:
    known = [t for t in item["tickers"] if t in order]
    return min(known, key=order.get) if known else MARKET


def make_batches(items: list[dict], order: dict[str, int], size: int, suffix: str = "batch") -> list[dict]:
    groups: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        groups[group_key(it, order)].append(it)
    batches = []
    for key in sorted(groups, key=lambda k: order.get(k, 999)):
        chunk = groups[key]
        for n, start in enumerate(range(0, len(chunk), size), 1):
            batches.append({"name": f"{key}_{suffix}{n:02d}", "key": key, "items": chunk[start:start + size]})
    return batches


def recent_items(conn, key: str, items: list[dict]) -> list[dict]:
    """Earlier items about the same company (or market), so Claude can spot differently worded copies."""
    ids = {it["id"] for it in items}
    times = [it["published_at"] for it in items if it["published_at"]]
    if not times:
        return []
    start = (datetime.fromisoformat(min(times).replace("Z", "+00:00")) - RECENT_WINDOW).strftime("%Y-%m-%dT%H:%M:%SZ")
    if key == MARKET:
        rows = conn.execute("""SELECT id AS item_id, title, published_at FROM items WHERE item_type = 'market_news'
                               AND published_at BETWEEN ? AND ? ORDER BY published_at DESC""", (start, max(times)))
    else:
        rows = conn.execute("""SELECT i.id AS item_id, i.title, i.published_at FROM items i JOIN item_tickers t ON t.item_id = i.id
                               WHERE t.ticker = ? AND i.published_at BETWEEN ? AND ? ORDER BY i.published_at DESC""",
                            (key, start, max(times)))
    return [dict(r) for r in rows if r["item_id"] not in ids][:RECENT_MAX]


def build_prompt(rules: str, payload: dict) -> str:
    return rules + json.dumps(payload, ensure_ascii=False, indent=1)


def load_rules(watchlist: list[dict]) -> str:
    wl = "\n".join(f"- {w['ticker']}: {w.get('name') or w['ticker']}" for w in watchlist)
    return PROMPT_PATH.read_text(encoding="utf-8").format(watchlist=wl, event_types=", ".join(EVENT_TYPES))


# ---------- 4-5. run, check, save ----------

def _log_run(conn, day: str, batch: dict, result, input_path: Path, output_path: Path | None, status: str,
             error: str | None) -> int:
    cur = conn.execute(
        """INSERT INTO llm_runs (task, run_day, batch, model, effort, prompt_version, item_count, status, seconds,
                                 input_path, output_path, error, usage, started_at)
           VALUES ('classify', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (day, batch["name"], claude_client.MODEL, claude_client.EFFORT, PROMPT_VERSION, len(batch["items"]), status,
         round(result.seconds, 1) if result else None, str(input_path), str(output_path) if output_path else None,
         error, json.dumps(result.usage) if result and result.usage else None, utc_now_z()))
    return cur.lastrowid


def save_label(conn, label, run_id: int) -> None:
    cur = conn.execute(
        """INSERT INTO item_labels (item_id, prompt_version, model, llm_run_id, relevant, tickers, event_type, importance,
                                    importance_reason, sentiment, fiscal_period, near_duplicate_of, one_line, labelled_at)
           VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
           ON CONFLICT (item_id, prompt_version) DO NOTHING""",
        (label.item_id, PROMPT_VERSION, claude_client.MODEL, run_id, int(label.relevant), json.dumps(label.tickers),
         label.event_type, label.importance, label.importance_reason, label.sentiment, label.fiscal_period,
         label.near_duplicate_of, label.one_line, utc_now_z()))
    if cur.rowcount:
        conn.executemany("INSERT INTO item_figures (label_id, item_id, label, value, quote) VALUES (?, ?, ?, ?, ?)",
                         [(cur.lastrowid, label.item_id, f.label, f.value, f.quote) for f in label.figures])
        conn.execute("UPDATE items SET relevance = ? WHERE id = ?",
                     ("relevant" if label.relevant else "irrelevant", label.item_id))


def process_reply(conn, day: str, batch: dict, result, input_path: Path, out_dir: Path, q_dir: Path,
                  watchlist: set[str]) -> tuple[int, dict[int, tuple[str, int, str | None]]]:
    """Check one batch's reply and save the good labels. Returns (saved, {item_id: (reason, run_id, output_path)})."""
    ids = {it["id"] for it in batch["items"]}
    if not result.ok:
        run_id = _log_run(conn, day, batch, result, input_path, None, "failed", result.error)
        return 0, {i: (f"claude call failed: {result.error}", run_id, None) for i in ids}
    try:
        reply = extract_json(result.text)
        if not isinstance(reply.get("items"), list):
            raise ValueError('reply has no "items" list')
    except Exception as exc:
        bad_path = q_dir / f"{batch['name']}_output.txt"
        bad_path.parent.mkdir(parents=True, exist_ok=True)
        bad_path.write_text(result.text, encoding="utf-8")
        run_id = _log_run(conn, day, batch, result, input_path, bad_path, "failed", f"unreadable reply: {exc}")
        return 0, {i: (f"unreadable reply: {exc}", run_id, str(bad_path)) for i in ids}

    output_path = out_dir / f"{batch['name']}_output.json"
    output_path.write_text(json.dumps(reply, ensure_ascii=False, indent=1), encoding="utf-8")
    known = ids | {r["item_id"] for r in batch["recent"]}
    good, failed, seen = [], {}, set()
    for raw in reply["items"]:
        label, reason = check_label(raw if isinstance(raw, dict) else {}, batch_ids=ids, known_ids=known,
                                    watchlist=watchlist, source_text=batch["source_text"])
        if label is None:
            item_id = raw.get("item_id") if isinstance(raw, dict) else None
            if item_id in ids and item_id not in seen:
                failed[item_id] = reason
            continue
        if label.item_id in seen:
            continue                    # answered twice: keep the first
        seen.add(label.item_id)
        failed.pop(label.item_id, None)
        good.append(label)
    for i in ids - seen:
        failed.setdefault(i, "no answer for this item")

    status = "ok" if not failed else ("partial" if good else "failed")
    run_id = _log_run(conn, day, batch, result, input_path, output_path, status,
                      "; ".join(f"{k}: {v}" for k, v in list(failed.items())[:5]) or None)
    for label in good:
        save_label(conn, label, run_id)
    conn.commit()
    return len(good), {i: (r, run_id, str(output_path)) for i, r in failed.items()}


def prepare(conn, batches: list[dict], rules: str, out_dir: Path) -> list[str]:
    prompts = []
    for b in batches:
        entries, b["source_text"] = [], {}
        for it in b["items"]:
            entry, text = build_entry(conn, it)
            entries.append(entry)
            b["source_text"][it["id"]] = text
        b["recent"] = recent_items(conn, b["key"], b["items"])
        payload = {"items": entries, "recent_items": b["recent"]}
        b["input_path"] = out_dir / f"{b['name']}_input.json"
        b["input_path"].write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        prompts.append(build_prompt(rules, payload))
    return prompts


def run(day: date, *, limit: int | None = None, ticker: str | None = None, dry_run: bool = False,
        retry_quarantine: bool = False, batch_size: int = BATCH_SIZE, max_parallel: int = MAX_PARALLEL) -> dict:
    conn = connect()
    watch_entries = load_watchlist_entries()
    order = {w["ticker"]: n for n, w in enumerate(watch_entries)}
    watchlist = set(order)
    rules = load_rules(watch_entries)

    stamp = datetime.now(timezone.utc).strftime("%H%M%SZ")
    out_dir = LLM_DIR / day.isoformat() / "classify" / stamp
    q_dir = LLM_DIR / day.isoformat() / "quarantine" / stamp
    items = pending_items(conn, limit=limit, ticker=ticker, retry_quarantine=retry_quarantine)
    summary = {"items": len(items), "labelled": 0, "quarantined": 0, "calls": 0, "folder": str(out_dir)}
    if not items:
        summary["folder"] = "(nothing to do, no files written)"
        conn.close()
        return summary
    out_dir.mkdir(parents=True, exist_ok=True)
    if retry_quarantine:
        conn.execute("UPDATE llm_quarantine SET resolved = 1 WHERE prompt_version = ? AND resolved = 0 AND item_id IN (%s)"
                     % ",".join("?" * len(items)), (PROMPT_VERSION, *[i["id"] for i in items]))
        conn.commit()

    by_id = {it["id"]: it for it in items}
    failed: dict[int, tuple[str, int, str | None]] = {}
    for round_no, suffix in ((1, "batch"), (2, "retry")):
        todo = list(by_id.values()) if round_no == 1 else [by_id[i] for i in failed]
        if not todo:
            break
        batches = make_batches(todo, order, batch_size, suffix)
        prompts = prepare(conn, batches, rules, out_dir)
        if dry_run:
            summary["batches_prepared"] = len(batches)
            conn.close()
            return summary
        results = asyncio.run(claude_client.run_many(prompts, max_parallel=max_parallel))
        summary["calls"] += len(results)
        failed = {}
        for b, res in zip(batches, results):
            saved, bad = process_reply(conn, day.isoformat(), b, res, b["input_path"], out_dir, q_dir, watchlist)
            summary["labelled"] += saved
            failed.update(bad)
            print(f"  {b['name']:<22} {len(b['items']):>3} items  {res.seconds:6.1f}s  "
                  f"saved {saved}" + (f", failed {len(bad)}" if bad else ""), flush=True)

    if failed:                          # failed twice -> quarantine
        q_dir.mkdir(parents=True, exist_ok=True)
        now = utc_now_z()
        conn.executemany("""INSERT INTO llm_quarantine (item_id, prompt_version, llm_run_id, reason, output_path, created_at)
                            VALUES (?, ?, ?, ?, ?, ?)""",
                         [(i, PROMPT_VERSION, run_id, reason, path, now) for i, (reason, run_id, path) in failed.items()])
        conn.commit()
        (q_dir / "quarantined_items.json").write_text(json.dumps(
            [{"item_id": i, "title": by_id[i]["title"], "reason": r, "output_path": p}
             for i, (r, _, p) in failed.items()], ensure_ascii=False, indent=1), encoding="utf-8")
        summary["quarantined"] = len(failed)
        summary["quarantine_folder"] = str(q_dir)
    conn.close()
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Label new items with Claude")
    parser.add_argument("--day", default="today", help="Folder date YYYY-MM-DD (default: today)")
    parser.add_argument("--limit", type=int, help="Only the first N pending items (for a test)")
    parser.add_argument("--ticker", help="Only items tagged with this ticker")
    parser.add_argument("--dry-run", action="store_true", help="Write the input files, do not call Claude")
    parser.add_argument("--retry-quarantine", action="store_true", help="Also retry items that are in quarantine")
    parser.add_argument("--batch-size", type=int, default=BATCH_SIZE)
    parser.add_argument("--max-parallel", type=int, default=MAX_PARALLEL)
    args = parser.parse_args()
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)

    print(f"Classify ({PROMPT_VERSION}, {claude_client.MODEL}, effort {claude_client.EFFORT})", flush=True)
    s = run(day, limit=args.limit, ticker=args.ticker, dry_run=args.dry_run, retry_quarantine=args.retry_quarantine,
            batch_size=args.batch_size, max_parallel=args.max_parallel)
    print(f"\nItems pending: {s['items']}  labelled: {s['labelled']}  quarantined: {s['quarantined']}  "
          f"Claude calls: {s['calls']}")
    if "batches_prepared" in s:
        print(f"Dry run: {s['batches_prepared']} batch input file(s) written")
    print(f"Files: {s['folder']}")
    if s.get("quarantine_folder"):
        print(f"Quarantine: {s['quarantine_folder']}")


if __name__ == "__main__":
    main()
