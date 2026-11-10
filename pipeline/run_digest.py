"""Milestone 6: the daily digest, written by Claude from a fact sheet and checked by Python.

    python -m pipeline.run_digest                     # today
    python -m pipeline.run_digest --day 2026-11-10
    python -m pipeline.run_digest --dry-run           # write the fact sheets only, no Claude

What happens:
  1. Python picks the items: relevant + high/medium importance (Claude's M5 labels), new since the last digest
     (first digest: last 7 days), published within 7 days, one per story (copies shown as "also reported by").
  2. Python builds a fact sheet per company with news (+ one for MARKET), each fact with an ID, from signals.db:
        N = news, S = SEC filing, F = Fed release, P = prices, R = analyst counts, E = next earnings, M = FRED
     Python works out every % change, so Claude never does arithmetic.
        -> data/digests/<day>/<SECTION>_input.json
  3. Claude (no tools) writes bullets from that sheet only: {"text": ..., "cites": [...]}.
        -> data/digests/<day>/<SECTION>_output.json (written by Python)
  4. Python checks every bullet: citations exist, every number is in a cited fact, no advice in its own voice.
     Fail -> sent back once with the reason -> still failing -> removed, listed under "Removed by fact check".
  5. Python adds the tables (built straight from the database), sources and disclaimer:
        -> data/digests/<day>/digest.md, digest.json, factcheck_report.json ; tables digests + digest_lines
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from checks.advice_guard import check_advice
from checks.citation_check import check_citations
from checks.factcheck import check_numbers
from connectors.base import PROJECT_ROOT, load_watchlist_entries, utc_now_z
from llm import claude_client
from llm.schemas import Bullet, extract_json
from pipeline.run_classify import PROMPT_VERSION as CLASSIFY_VERSION
from pipeline.run_classify import build_entry
from storage.db import connect

PROMPT_VERSION = "digest-v1"             # bump when llm/prompts/digest.md changes
PROMPT_PATH = PROJECT_ROOT / "llm" / "prompts" / "digest.md"
DIGEST_DIR = PROJECT_ROOT / "data" / "digests"
MARKET = "MARKET"
MAX_AGE = timedelta(days=7)              # items published more than 7 days before the digest day are left out
FRESH_LIMIT = timedelta(hours=24)        # a source not updated for longer is flagged
NEWS_CHARS = 800
UPCOMING_DAYS = 14
DISCLAIMER = "**SIMULATION, NOT INVESTMENT ADVICE.**"
MARKET_SERIES = ("DFEDTARU", "DFF", "DGS2", "DGS10", "T10Y2Y", "VIXCLS", "BAMLH0A0HYM2", "DCOILWTICO",
                 "CPIAUCSL", "UNRATE", "PAYEMS")
BENCHMARKS = {"^GSPC": "S&P 500", "^NDX": "Nasdaq-100"}


def _z(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _num(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"


# ---------- 1. which items ----------

def previous_digest(conn, day: date):
    return conn.execute("SELECT * FROM digests WHERE day < ? AND status != 'blocked' ORDER BY day DESC LIMIT 1",
                        (day.isoformat(),)).fetchone()


def select_items(conn, day: date, since: str | None, max_age: timedelta = MAX_AGE) -> list[dict]:
    end = f"{day.isoformat()}T23:59:59Z"
    start = _z(datetime.combine(day, datetime.min.time(), timezone.utc) - max_age)
    rows = conn.execute(
        """SELECT i.*, l.tickers AS label_tickers, l.importance, l.event_type FROM items i
           JOIN item_labels l ON l.item_id = i.id AND l.prompt_version = ?
           WHERE l.relevant = 1 AND l.importance IN ('high', 'medium') AND l.near_duplicate_of IS NULL
             AND i.published_at BETWEEN ? AND ? AND (? IS NULL OR i.first_seen_at >= ?)  -- >=: same second = shown again, never lost
           ORDER BY CASE l.importance WHEN 'high' THEN 0 ELSE 1 END, i.published_at DESC""",
        (CLASSIFY_VERSION, start, end, since, since)).fetchall()
    out = []
    for r in rows:
        it = dict(r)
        it["extra"] = json.loads(it["extra"] or "{}")
        it["label_tickers"] = json.loads(it["label_tickers"] or "[]")
        it["tickers"] = it["label_tickers"]
        it["also_reported_by"] = copies_of(conn, it)
        out.append(it)
    return out


def copies_of(conn, it: dict) -> list[str]:
    """Publishers of the other copies of this story (Claude's near_duplicate_of + the headline-rule events)."""
    rows = conn.execute(
        """SELECT DISTINCT i.publisher FROM items i LEFT JOIN item_labels l ON l.item_id = i.id AND l.prompt_version = ?
           WHERE i.id != ? AND (l.near_duplicate_of = ? OR (i.event_id IS NOT NULL AND i.event_id = ?))""",
        (CLASSIFY_VERSION, it["id"], it["id"], it["event_id"])).fetchall()
    return sorted({r["publisher"] for r in rows if r["publisher"] and r["publisher"] != it["publisher"]})


def unlabelled_count(conn) -> int:
    return conn.execute("""SELECT COUNT(*) FROM items i WHERE NOT EXISTS
                           (SELECT 1 FROM item_labels l WHERE l.item_id = i.id AND l.prompt_version = ?)
                           AND (i.source != 'sec_edgar' OR i.extra NOT LIKE '%"is_backfill": true%')""",
                        (CLASSIFY_VERSION,)).fetchone()[0]


# ---------- 2. facts from the database ----------

def item_fact(conn, it: dict) -> dict:
    _, text = build_entry(conn, it)
    if it["source"] == "sec_edgar":
        fid, ftype = f"S{it['id']}", "sec_filing"
    elif it["source"] == "federal_reserve":
        fid, ftype = f"F{it['id']}", "fed_release"
    else:
        fid, ftype, text = f"N{it['id']}", it["item_type"], text[:NEWS_CHARS]
    fact = {"id": fid, "type": ftype, "importance": it["importance"], "published_at": it["published_at"],
            "publisher": it["publisher"], "text": text}
    if it["also_reported_by"]:
        fact["also_reported_by"] = it["also_reported_by"]
    form = it["item_type"] if ftype == "sec_filing" else None
    source = {"sec_filing": f"SEC {form}, {it['extra'].get('company_name')}",
              "fed_release": "Federal Reserve"}.get(ftype, it["publisher"] or it["source"])
    fact["_source"] = {"label": f"{source}: {it['title']}", "title": it["title"], "publisher": source,
                       "date": (it["published_at"] or "")[:10], "url": it["url"], "also": it["also_reported_by"]}
    return fact


def price_stats(conn, ticker: str, day: date) -> dict | None:
    rows = conn.execute("""SELECT date, close, adj_close, high, volume, is_final FROM prices
                           WHERE ticker = ? AND date <= ? ORDER BY date DESC LIMIT 300""",
                        (ticker, day.isoformat())).fetchall()
    if len(rows) < 2:
        return None
    last = rows[0]
    adj = lambda r: r["adj_close"] or r["close"]
    pct = lambda a, b: (adj(a) / adj(b) - 1) * 100
    year_base = next((r for r in rows if r["date"] < f"{last['date'][:4]}-01-01"), None)
    cutoff = (date.fromisoformat(last["date"]) - timedelta(days=365)).isoformat()
    high52 = max(r["high"] or r["close"] for r in rows if r["date"] > cutoff)
    vols = [r["volume"] for r in rows[1:21] if r["volume"]]
    return {"ticker": ticker, "date": last["date"], "close": last["close"], "final": bool(last["is_final"]),
            "d1": pct(last, rows[1]), "d5": pct(last, rows[5]) if len(rows) > 5 else None,
            "ytd": pct(last, year_base) if year_base else None,
            "from_high": (last["close"] / high52 - 1) * 100, "high52": high52,
            "vol_ratio": (last["volume"] / (sum(vols) / len(vols))) if vols and last["volume"] else None}


def price_fact(p: dict, name: str) -> dict:
    parts = [f"{name} close on {p['date']}: ${p['close']:,.2f}" + ("" if p["final"] else " (session still open when fetched)"),
             f"1-day {p['d1']:+.2f}%"]
    if p["d5"] is not None:
        parts.append(f"5-day {p['d5']:+.2f}%")
    if p["ytd"] is not None:
        parts.append(f"year-to-date {p['ytd']:+.2f}%")
    parts.append(f"{abs(p['from_high']):.2f}% below the 52-week high of ${p['high52']:,.2f}")
    if p["vol_ratio"]:
        parts.append(f"volume {p['vol_ratio']:.2f}x the 20-day average")
    return {"id": f"P:{p['ticker']}", "type": "price", "text": ". ".join(parts[:1]) + ". " + ", ".join(parts[1:]) + ".",
            "_source": {"label": f"yfinance daily prices, {p['ticker']}", "date": p["date"],
                        "url": f"https://finance.yahoo.com/quote/{p['ticker']}"}}


def ratings(conn, ticker: str) -> list:
    return conn.execute("""SELECT * FROM analyst_ratings WHERE ticker = ? ORDER BY period DESC LIMIT 2""",
                        (ticker,)).fetchall()


def _counts(r) -> str:
    return (f"{r['strong_buy']} strong buy, {r['buy']} buy, {r['hold']} hold, {r['sell']} sell, "
            f"{r['strong_sell']} strong sell")


def ratings_fact(ticker: str, rows: list) -> dict | None:
    if not rows:
        return None
    text = f"Finnhub analyst recommendation counts for {ticker}, {rows[0]['period'][:7]}: {_counts(rows[0])}."
    if len(rows) > 1:
        text += f" Previous month ({rows[1]['period'][:7]}): {_counts(rows[1])}."
    return {"id": f"R:{ticker}", "type": "analyst_counts", "text": text,
            "_source": {"label": f"Finnhub recommendation trends, {ticker}", "date": rows[0]["period"][:7],
                        "url": "https://finnhub.io/docs/api/recommendation-trends"}}


def next_earnings(conn, ticker: str, day: date):
    return conn.execute("""SELECT * FROM earnings_calendar WHERE ticker = ? AND report_date >= ?
                           ORDER BY report_date LIMIT 1""", (ticker, day.isoformat())).fetchone()


def earnings_fact(ticker: str, e) -> dict | None:
    if not e:
        return None
    text = f"Next earnings report for {ticker} (Finnhub calendar): {e['report_date']}"
    text += f", {e['timing']}" if e["timing"] else ""
    if e["eps_estimate"] is not None:
        text += f"; consensus EPS estimate {e['eps_estimate']:.2f}"
    if e["revenue_estimate"]:
        text += f"; revenue estimate ${e['revenue_estimate'] / 1e6:,.1f}M"
    return {"id": f"E:{ticker}", "type": "next_earnings", "text": text + ".",
            "_source": {"label": f"Finnhub earnings calendar, {ticker}", "date": e["report_date"],
                        "url": "https://finnhub.io/docs/api/earnings-calendar"}}


def macro_rows(conn, day: date) -> list[dict]:
    out = []
    for sid in MARKET_SERIES:
        info = conn.execute("SELECT * FROM series_info WHERE series_id = ?", (sid,)).fetchone()
        obs = conn.execute("""SELECT date, value FROM macro_series WHERE series_id = ? AND date <= ?
                              ORDER BY date DESC LIMIT 2""", (sid, day.isoformat())).fetchall()
        if info and obs:
            out.append({"series_id": sid, "label": info["label"], "units": info["units"], "date": obs[0]["date"],
                        "value": obs[0]["value"], "prev_date": obs[1]["date"] if len(obs) > 1 else None,
                        "prev": obs[1]["value"] if len(obs) > 1 else None})
    return out


def macro_fact(m: dict) -> dict:
    text = f"{m['label']} ({m['series_id']}): {_num(m['value'])} on {m['date']}"
    if m["prev"] is not None:
        text += f" (previous {_num(m['prev'])} on {m['prev_date']})"
    return {"id": f"M:{m['series_id']}", "type": "macro", "text": text + f". Units: {m['units']}.",
            "_source": {"label": f"FRED {m['series_id']}: {m['label']}", "date": m["date"],
                        "url": f"https://fred.stlouisfed.org/series/{m['series_id']}"}}


def build_sheets(conn, day: date, items: list[dict], watch: list[dict]) -> tuple[dict, dict]:
    """Return ({section: [facts]}, data for the Python tables)."""
    by_section: dict[str, list[dict]] = defaultdict(list)
    for it in items:
        for key in (it["label_tickers"] or [MARKET]):
            by_section[key].append(item_fact(conn, it))

    names = {w["ticker"]: w.get("name") or w["ticker"] for w in watch}
    tables = {"prices": {}, "ratings": {}, "earnings": {}, "macro": macro_rows(conn, day), "benchmarks": {}}
    for t in names:
        tables["prices"][t] = price_stats(conn, t, day)
        tables["ratings"][t] = ratings(conn, t)
        tables["earnings"][t] = next_earnings(conn, t, day)
    for sym in BENCHMARKS:
        tables["benchmarks"][sym] = price_stats(conn, sym, day)

    sheets = {}
    for key, facts in by_section.items():
        if key == MARKET:
            extra = [macro_fact(m) for m in tables["macro"]]
            extra += [price_fact(p, BENCHMARKS[s]) for s, p in tables["benchmarks"].items() if p]
        else:
            p = tables["prices"].get(key)
            extra = [f for f in (price_fact(p, key) if p else None, ratings_fact(key, tables["ratings"].get(key)),
                                 earnings_fact(key, tables["earnings"].get(key))) if f]
        sheets[key] = facts + extra
    return sheets, tables


# ---------- 3-4. Claude writes, Python checks ----------

def public_sheet(section: str, facts: list[dict], names: dict) -> dict:
    title = "Market and macro" if section == MARKET else f"{section} ({names.get(section, section)})"
    return {"section": title, "facts": [{k: v for k, v in f.items() if not k.startswith("_")} for f in facts]}


def make_prompt(rules: str, sheet: dict, failed: list[dict] | None = None) -> str:
    prompt = rules + json.dumps(sheet, ensure_ascii=False, indent=1)
    if failed:
        listed = [{"index": n, "text": f["text"], "cites": f["cites"], "problem": f["problem"]}
                  for n, f in enumerate(failed)]
        prompt += ("\n\n## Fix these bullets\nThese bullets you wrote failed the checks. Rewrite each one so it "
                   "passes the rules, or leave it out if it cannot be fixed with the facts given. Reply in the same "
                   'JSON format with only the rewritten bullets, each with "fixes": the index of the bullet it replaces.\n'
                   + json.dumps(listed, ensure_ascii=False, indent=1))
    return prompt


def check_bullet(raw, facts_by_id: dict[str, dict]) -> tuple[Bullet | None, str | None]:
    try:
        b = Bullet.model_validate(raw)
    except Exception as exc:
        return None, f"schema: {str(exc)[:300]}"
    reason = check_citations(b.cites, set(facts_by_id))
    if reason:
        return b, reason
    cited = [facts_by_id[c] for c in b.cites]
    reason = check_numbers(b.text, [f["text"] for f in cited])
    if reason:
        return b, reason
    return b, check_advice(b.text, {f["type"] for f in cited})


def read_reply(result) -> tuple[list, str | None]:
    if not result.ok:
        return [], f"claude call failed: {result.error}"
    try:
        reply = extract_json(result.text)
        bullets = reply.get("bullets")
        if not isinstance(bullets, list):
            raise ValueError('no "bullets" list')
        return bullets, None
    except Exception as exc:
        return [], f"unreadable reply: {exc}"


def log_llm(conn, day: str, batch: str, result, n: int, in_path: Path, out_path: Path | None, status: str,
            error: str | None) -> None:
    conn.execute(
        """INSERT INTO llm_runs (task, run_day, batch, model, effort, prompt_version, item_count, status, seconds,
                                 input_path, output_path, error, usage, started_at)
           VALUES ('digest', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (day, batch, claude_client.MODEL, claude_client.EFFORT, PROMPT_VERSION, n, status,
         round(result.seconds, 1), str(in_path), str(out_path) if out_path else None, error,
         json.dumps(result.usage) if result.usage else None, utc_now_z()))


def write_sections(conn, day: date, sheets: dict, names: dict, out_dir: Path) -> dict:
    """Claude writes each section; Python checks; one retry for failed bullets. Returns per-section results."""
    rules = PROMPT_PATH.read_text(encoding="utf-8").format()
    sections = list(sheets)
    public = {s: public_sheet(s, sheets[s], names) for s in sections}
    facts_by_id = {s: {f["id"]: f for f in sheets[s]} for s in sections}
    for s in sections:
        (out_dir / f"{s}_input.json").write_text(json.dumps(public[s], ensure_ascii=False, indent=1), encoding="utf-8")

    results = {s: {"kept": [], "removed": [], "error": None} for s in sections}
    pending: dict[str, list[dict]] = {}            # section -> bullets that failed round 1
    whole_again: set[str] = set()                  # sections whose round-1 call failed: ask again from scratch
    for attempt in (1, 2):
        todo = sections if attempt == 1 else [s for s in sections if pending.get(s) or s in whole_again]
        if not todo:
            break
        prompts = [make_prompt(rules, public[s], None if (attempt == 1 or s in whole_again) else pending[s])
                   for s in todo]
        replies = asyncio.run(claude_client.run_many(prompts))
        for s, res in zip(todo, replies):
            suffix = "" if attempt == 1 else "_retry"
            bullets, error = read_reply(res)
            out_path = out_dir / f"{s}_output{suffix}.json"
            out_path.write_text(json.dumps({"bullets": bullets} if not error else {"error": error, "raw": res.text},
                                           ensure_ascii=False, indent=1), encoding="utf-8")
            failed, fixed = [], set()
            for raw in bullets:
                b, reason = check_bullet(raw, facts_by_id[s])
                entry = ({"text": str(raw)[:300], "cites": [], "problem": reason} if b is None
                         else {"text": b.text, "cites": b.cites, "problem": reason})
                if b is not None and b.fixes is not None:
                    entry["fixes"] = b.fixes
                if b is not None and reason is None:
                    results[s]["kept"].append({"text": b.text, "cites": b.cites, "attempt": attempt})
                    if b.fixes is not None:
                        fixed.add(b.fixes)
                else:
                    failed.append(entry)

            if attempt == 1:
                if error:
                    whole_again.add(s)
                else:
                    pending[s] = failed
            elif s in whole_again:
                results[s]["error"] = error            # still failing: the section has no bullets
                results[s]["removed"] += [{**f, "attempt": 2} for f in failed]
            else:
                retry_reason = {f.get("fixes"): f["problem"] for f in failed if f.get("fixes") is not None}
                for n, orig in enumerate(pending[s]):
                    if n in fixed:
                        continue
                    why = retry_reason.get(n, "not fixed in the retry") if not error else f"retry failed: {error}"
                    results[s]["removed"].append({**orig, "attempt": 2, "problem": f"{orig['problem']}; then {why}"})
            status = "failed" if error else ("partial" if failed else "ok")
            log_llm(conn, day.isoformat(), f"{s}{suffix}", res, len(sheets[s]), out_dir / f"{s}_input.json",
                    out_path, status, error or ("; ".join(f["problem"] for f in failed[:3]) or None))
        conn.commit()
    return results


# ---------- 5. render ----------

def render(day: date, built_at: str, since: str | None, watch: list[dict], sheets: dict, results: dict,
           tables: dict, freshness: list[dict], upcoming: list) -> tuple[str, dict]:
    names = {w["ticker"]: w.get("name") or w["ticker"] for w in watch}
    all_facts = {f["id"]: f for facts in sheets.values() for f in facts}
    refs: dict[str, int] = {}

    def ref(fid: str) -> str:
        refs.setdefault(fid, len(refs) + 1)
        return f"[{refs[fid]}]"

    def bullet_lines(section: str) -> list[str]:
        out = []
        for b in results[section]["kept"]:
            out.append(f"- {b['text']} {''.join(ref(c) for c in b['cites'])}")
            also = sorted({p for c in b["cites"] for p in all_facts[c].get("also_reported_by", [])})
            if also:
                out.append(f"  *Also reported by: {', '.join(also)}*")
        if results[section]["error"]:
            out.append(f"- *Bullets for this section are unavailable: {results[section]['error'][:150]}*")
        return out

    L = [f"# Daily digest: {day.isoformat()}", "",
         f"{DISCLAIMER} Built {built_at} (UTC) from `signals.db`. Covers items "
         + (f"first seen after {since}" if since else "from the last 7 days (first digest)")
         + ". Numbers in tables come straight from the database; every number in a bullet was checked against "
           "the source it cites.", ""]

    L += ["## Market and macro", ""]
    if MARKET in results:
        L += bullet_lines(MARKET) + [""]
    L += ["| Indicator | Latest | Date | Previous |", "|---|---|---|---|"]
    for sym, p in tables["benchmarks"].items():
        if p:
            L.append(f"| {BENCHMARKS[sym]} | {p['close']:,.2f} ({p['d1']:+.2f}% 1d) | {p['date']} | |")
    for m in tables["macro"]:
        prev = f"{_num(m['prev'])} ({m['prev_date']})" if m["prev"] is not None else ""
        L.append(f"| {m['label']} ({m['series_id']}) | {_num(m['value'])} | {m['date']} | {prev} |")
    L.append("")

    L += ["## Watchlist snapshot", "",
          "| Ticker | Close | Date | 1d | 5d | YTD | vs 52w high | Volume vs 20d | Analyst counts SB/B/H/S/SS (Finnhub) | Next earnings |",
          "|---|---|---|---|---|---|---|---|---|---|"]
    fmt = lambda v: f"{v:+.2f}%" if v is not None else ""
    for t in names:
        p, r, e = tables["prices"].get(t), tables["ratings"].get(t), tables["earnings"].get(t)
        counts = (f"{r[0]['strong_buy']}/{r[0]['buy']}/{r[0]['hold']}/{r[0]['sell']}/{r[0]['strong_sell']} "
                  f"({r[0]['period'][:7]})") if r else ""
        nxt = f"{e['report_date']}" + (f" ({e['timing']})" if e and e["timing"] else "") if e else ""
        if p:
            L.append(f"| {t} | {p['close']:,.2f}{'' if p['final'] else '*'} | {p['date']} | {fmt(p['d1'])} | "
                     f"{fmt(p['d5'])} | {fmt(p['ytd'])} | {p['from_high']:+.2f}% | "
                     f"{(str(round(p['vol_ratio'], 2)) + 'x') if p['vol_ratio'] else ''} | {counts} | {nxt} |")
        else:
            L.append(f"| {t} | no price data | | | | | | | {counts} | {nxt} |")
    if any(p and not p["final"] for p in tables["prices"].values()):
        L.append("\n\\* session still open when prices were fetched")
    L.append("")

    L += ["## Company news", ""]
    quiet = []
    for t in names:
        if t in results:
            L += [f"### {t}: {names[t]}", ""] + bullet_lines(t) + [""]
        else:
            quiet.append(t)
    if quiet:
        L += [f"No new relevant news: {', '.join(quiet)}.", ""]

    L += [f"## Coming up (next {UPCOMING_DAYS} days)", ""]
    L += [f"- {r['ticker']} earnings {r['report_date']}" + (f" ({r['timing']})" if r["timing"] else "")
          for r in upcoming] or ["- No watchlist earnings dates in the Finnhub calendar."]
    L.append("")

    L += ["## Data freshness", ""]
    for f in freshness:
        flag = " **STALE**" if f["stale"] else ""
        L.append(f"- {f['connector']}: last run {f['finished_at'] or 'never'} ({f['status'] or '-'}){flag}")
    L.append("")

    L += ["## Sources", ""]
    for fid, n in refs.items():
        s = all_facts[fid]["_source"]
        L.append(f"{n}. {s['label']} ({s['date']}) {s['url'] or ''}".rstrip())
    L.append("")

    removed = [(s, r) for s in results for r in results[s]["removed"]]
    if removed:
        L += [f"## Removed by fact check ({len(removed)})", ""]
        L += [f"- [{s}] {r['text'][:200]} — *{r['problem']}*" for s, r in removed]
        L.append("")
    L += ["---", f"{DISCLAIMER} Generated automatically for a personal research project."]

    data = {"day": day.isoformat(), "built_at": built_at, "covers_since": since,
            "sections": {s: results[s] for s in results},
            "sources": {n: {"id": fid, **all_facts[fid]["_source"]} for fid, n in refs.items()}}
    return "\n".join(L) + "\n", data


def freshness_rows(conn, built_at: str) -> list[dict]:
    now = datetime.fromisoformat(built_at.replace("Z", "+00:00"))
    out = []
    for r in conn.execute("""SELECT connector, MAX(finished_at) finished_at FROM fetch_runs
                             WHERE status IN ('ok', 'degraded') GROUP BY connector ORDER BY connector"""):
        last = conn.execute("SELECT status FROM fetch_runs WHERE connector = ? ORDER BY id DESC LIMIT 1",
                            (r["connector"],)).fetchone()
        age = now - datetime.fromisoformat(r["finished_at"].replace("Z", "+00:00")) if r["finished_at"] else None
        out.append({"connector": r["connector"], "finished_at": r["finished_at"], "status": last["status"],
                    "stale": age is None or age > FRESH_LIMIT or last["status"] == "failed"})
    return out


def save_digest(conn, day: date, built_at: str, since, status: str, items_used: int, results: dict, path) -> int:
    old = conn.execute("SELECT id FROM digests WHERE day = ?", (day.isoformat(),)).fetchone()
    if old:
        conn.execute("DELETE FROM digest_lines WHERE digest_id = ?", (old["id"],))
        conn.execute("DELETE FROM digests WHERE id = ?", (old["id"],))
    kept = sum(len(r["kept"]) for r in results.values())
    removed = sum(len(r["removed"]) for r in results.values())
    cur = conn.execute(
        """INSERT INTO digests (day, status, built_at, covers_since, items_used, bullets_kept, bullets_removed,
                                model, prompt_version, path) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (day.isoformat(), status, built_at, since, items_used, kept, removed, claude_client.MODEL, PROMPT_VERSION,
         str(path) if path else None))
    did = cur.lastrowid
    for s, r in results.items():
        rows = [(did, s, n, b["text"], json.dumps(b["cites"]), "kept", None, b["attempt"]) for n, b in enumerate(r["kept"])]
        rows += [(did, s, 100 + n, b["text"], json.dumps(b["cites"]), "removed", b["problem"], b["attempt"])
                 for n, b in enumerate(r["removed"])]
        conn.executemany("""INSERT INTO digest_lines (digest_id, section, seq, text, cites, status, reason, attempt)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?)""", rows)
    conn.commit()
    return did


def run(day: date, *, dry_run: bool = False) -> dict:
    conn = connect()
    watch = load_watchlist_entries()
    names = {w["ticker"]: w.get("name") or w["ticker"] for w in watch}
    built_at = utc_now_z()
    prev = previous_digest(conn, day)
    since = prev["built_at"] if prev else None
    items = select_items(conn, day, since)
    out_dir = DIGEST_DIR / day.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)
    summary = {"items": len(items), "unlabelled": unlabelled_count(conn), "folder": str(out_dir)}

    sheets, tables = build_sheets(conn, day, items, watch)
    summary["sections"] = list(sheets)
    if dry_run:
        for s in sheets:
            (out_dir / f"{s}_input.json").write_text(json.dumps(public_sheet(s, sheets[s], names), ensure_ascii=False,
                                                                indent=1), encoding="utf-8")
        conn.close()
        return summary

    results = write_sections(conn, day, sheets, names, out_dir)
    upcoming = conn.execute("""SELECT ticker, report_date, timing FROM earnings_calendar
                               WHERE report_date BETWEEN ? AND ? AND ticker IN (%s) ORDER BY report_date"""
                            % ",".join("?" * len(names)),
                            (day.isoformat(), (day + timedelta(days=UPCOMING_DAYS)).isoformat(), *names)).fetchall()
    status = "partial" if any(r["error"] for r in results.values()) else "ok"
    try:
        md, data = render(day, built_at, since, watch, sheets, results, tables, freshness_rows(conn, built_at), upcoming)
        if DISCLAIMER not in md:
            raise ValueError("disclaimer missing")
    except Exception as exc:                      # a Python part is broken: block, never send half a digest
        save_digest(conn, day, built_at, since, "blocked", len(items), results, None)
        conn.close()
        raise RuntimeError(f"digest blocked: {exc}") from exc

    path = out_dir / "digest.md"
    path.write_text(md, encoding="utf-8")
    data["status"] = status
    (out_dir / "digest.json").write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
    report = [{"section": s, "status": st, **b} for s, r in results.items()
              for st, lst in (("kept", r["kept"]), ("removed", r["removed"])) for b in lst]
    (out_dir / "factcheck_report.json").write_text(json.dumps(report, ensure_ascii=False, indent=1), encoding="utf-8")
    save_digest(conn, day, built_at, since, status, len(items), results, path)
    conn.close()
    summary.update(status=status, path=str(path), kept=sum(len(r["kept"]) for r in results.values()),
                   removed=sum(len(r["removed"]) for r in results.values()),
                   retried=sum(1 for r in results.values() for b in r["kept"] if b["attempt"] == 2))
    return summary


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the daily digest")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD (default: today)")
    parser.add_argument("--dry-run", action="store_true", help="Write the fact sheets only, do not call Claude")
    args = parser.parse_args()
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)
    print(f"Digest {day} ({PROMPT_VERSION}, {claude_client.MODEL})", flush=True)
    s = run(day, dry_run=args.dry_run)
    if s["unlabelled"]:
        print(f"  Note: {s['unlabelled']} item(s) have no label yet; run `python -m pipeline.run_classify` first.")
    print(f"  Items used: {s['items']}  sections: {', '.join(s['sections']) or '-'}")
    if args.dry_run:
        print(f"  Dry run: fact sheets in {s['folder']}")
        return
    print(f"  Status: {s['status']}  bullets kept: {s['kept']} (of which fixed in retry: {s['retried']})  "
          f"removed: {s['removed']}")
    print(f"  Digest: {s['path']}")


if __name__ == "__main__":
    main()
