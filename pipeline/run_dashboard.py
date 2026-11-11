"""Milestone 6b: the daily dashboard (one offline HTML file with Plotly charts).

    python -m pipeline.run_dashboard                    # today (run run_digest first: the news table comes from it)
    python -m pipeline.run_dashboard --day 2026-11-10
    python -m pipeline.run_dashboard --no-claude        # charts + news table only, no narratives

Layout: right half = news table (rows = the checked digest bullets: ticker, date, title, summary, links);
left top = price chart per stock (all downloaded history, period buttons) + Claude's last-month narrative;
left bottom = Fed / macro chart per FRED series + Claude's big picture and a note per series.

Claude never sees the database: Python builds fact sheets (every change already computed), Claude writes
sentences with citations, and Python checks every sentence exactly like the digest (citations exist, every
number is in a cited fact, no advice). Failed sentences get one retry, then they are dropped.
    -> data/dashboards/<day>/dashboard.html (+ dashboard.json, <PART>_input.json, <PART>_output.json)
"""
from __future__ import annotations

import argparse
import asyncio
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import PROJECT_ROOT, load_watchlist_entries, utc_now_z
from llm import claude_client
from llm.schemas import extract_json
from pipeline import deep_dive as DD
from pipeline import fundamentals as F
from pipeline import run_digest
from pipeline.run_digest import (BENCHMARKS, check_bullet, earnings_fact, freshness_rows, item_fact, next_earnings,
                                 ratings, ratings_fact)
from storage.db import connect

PROMPT_VERSION = "dashboard-v1"         # bump when llm/prompts/dashboard_stock.md / dashboard_macro.md change
FIN_PROMPT_VERSION = "fin-v1"           # bump when llm/prompts/dashboard_fin.md changes (re-writes the cached analyses)
FIN_MODEL = "claude-sonnet-5-5"         # user decision: Sonnet for the financial statement analysis
STOCK_MODEL = claude_client.MODEL       # Haiku 4.5
MACRO_MODEL = "claude-sonnet-5-5"       # user decision: Sonnet for the big-picture macro analysis
PROMPTS = PROJECT_ROOT / "llm" / "prompts"
TEMPLATE = Path(__file__).with_name("dashboard_template.html")
DASH_DIR = PROJECT_ROOT / "data" / "dashboards"
STOCK_NEWS_DAYS = 30
MACRO_NEWS_DAYS = 7
FED_DAYS = 30
MAX_STOCK_NEWS = 20
MAX_MACRO_NEWS = 15

# How each FRED series is drawn. kind: line | step | area | bars | ma ; transform makes the bars meaningful.
SERIES_CHART = {
    "DFEDTARU": {"kind": "step", "suffix": "%"},
    "DFF": {"kind": "step", "suffix": "%"},
    "DGS3MO": {"kind": "line", "suffix": "%"},
    "DGS2": {"kind": "line", "suffix": "%"},
    "DGS10": {"kind": "line", "suffix": "%"},
    "T10Y2Y": {"kind": "area", "suffix": "%", "zero": True},
    "DFII10": {"kind": "line", "suffix": "%", "zero": True},
    "T5YIE": {"kind": "line", "suffix": "%"},
    "CPIAUCSL": {"kind": "bars", "suffix": "%", "transform": "yoy", "label_suffix": " (year-over-year %)"},
    "CPILFESL": {"kind": "bars", "suffix": "%", "transform": "yoy", "label_suffix": " (year-over-year %)"},
    "PCEPILFE": {"kind": "bars", "suffix": "%", "transform": "yoy", "label_suffix": " (year-over-year %)"},
    "UNRATE": {"kind": "line", "suffix": "%"},
    "PAYEMS": {"kind": "bars", "transform": "diff", "label_suffix": " (monthly change, thousands)", "zero": True},
    "ICSA": {"kind": "ma", "transform": "ma4"},
    "GDPC1": {"kind": "bars", "suffix": "%", "transform": "qoq_ann", "label_suffix": " (quarterly change, annualised %)",
              "zero": True},
    "VIXCLS": {"kind": "line", "bands": [[0, 20, "#1e9e6a"], [20, 30, "#c98a0b"], [30, 90, "#d64545"]]},
    "BAMLH0A0HYM2": {"kind": "area", "suffix": "%"},
    "DTWEXBGS": {"kind": "line"},
    "DCOILWTICO": {"kind": "line", "prefix": "$"},
}
CURVE = (("DGS3MO", "3M"), ("DGS2", "2Y"), ("DGS10", "10Y"))


def _f(v: float) -> str:
    return f"{v:,.0f}" if abs(v) >= 1000 else f"{v:,.2f}"


def _back(rows: list, last_date: str, days: int):
    """Latest row on or before last_date - days."""
    target = (date.fromisoformat(last_date) - timedelta(days=days)).isoformat()
    earlier = [r for r in rows if r[0] <= target]
    return earlier[-1] if earlier else None


# ---------- chart data ----------

def price_history(conn, ticker: str, day: date) -> dict | None:
    rows = conn.execute("""SELECT date, open, high, low, close, adj_close, volume, is_final FROM prices
                           WHERE ticker = ? AND date <= ? ORDER BY date""", (ticker, day.isoformat())).fetchall()
    if not rows:
        return None
    return {"date": [r["date"] for r in rows], "open": [r["open"] for r in rows], "high": [r["high"] for r in rows],
            "low": [r["low"] for r in rows], "close": [r["close"] for r in rows],
            "adj": [r["adj_close"] or r["close"] for r in rows], "volume": [r["volume"] for r in rows],
            "final": bool(rows[-1]["is_final"])}


def series_history(conn, sid: str, day: date) -> list[tuple[str, float]]:
    return [(r["date"], r["value"]) for r in conn.execute(
        "SELECT date, value FROM macro_series WHERE series_id = ? AND date <= ? ORDER BY date", (sid, day.isoformat()))]


def transform(hist: list[tuple[str, float]], how: str | None) -> list[tuple]:
    """Return [(date, value, extra)] after the chart transform."""
    if how == "yoy":         # monthly index -> % change vs 12 months earlier
        return [(d, (v / hist[i - 12][1] - 1) * 100, None) for i, (d, v) in enumerate(hist) if i >= 12]
    if how == "diff":        # payrolls level -> monthly change
        return [(d, v - hist[i - 1][1], None) for i, (d, v) in enumerate(hist) if i >= 1]
    if how == "qoq_ann":     # GDP level -> annualised quarterly % change
        return [(d, ((v / hist[i - 1][1]) ** 4 - 1) * 100, None) for i, (d, v) in enumerate(hist) if i >= 1]
    if how == "ma4":         # weekly claims + 4-week average
        return [(d, v, sum(x for _, x in hist[i - 3:i + 1]) / 4 if i >= 3 else None) for i, (d, v) in enumerate(hist)]
    return [(d, v, None) for d, v in hist]


def series_chart(conn, sid: str, info, day: date) -> dict | None:
    hist = series_history(conn, sid, day)
    if not hist:
        return None
    cfg = SERIES_CHART.get(sid, {"kind": "line"})
    pts = transform(hist, cfg.get("transform"))
    ys = [[round(p[1], 4) for p in pts]]
    if cfg.get("transform") == "ma4":
        ys.append([round(p[2], 1) if p[2] is not None else None for p in pts])
    return {"label": info["label"] + cfg.get("label_suffix", ""), "group": info["grp"], "unit": info["units"],
            "kind": cfg["kind"], "x": [p[0] for p in pts], "ys": ys, "suffix": cfg.get("suffix", ""),
            "prefix": cfg.get("prefix", ""), "zero": cfg.get("zero", False), "bands": cfg.get("bands", [])}


def curve_chart(conn, day: date) -> tuple[dict | None, dict | None]:
    hists = {sid: series_history(conn, sid, day) for sid, _ in CURVE}
    if not all(hists.values()):
        return None, None
    common = sorted(set.intersection(*(set(d for d, _ in h) for h in hists.values())))
    if not common:
        return None, None
    last = common[-1]
    lines, parts = [], []
    for label, days in (("Now", 0), ("3 months ago", 91), ("1 year ago", 365)):
        target = (date.fromisoformat(last) - timedelta(days=days)).isoformat()
        d = max((x for x in common if x <= target), default=None)
        if not d:
            continue
        vals = [dict(hists[sid])[d] for sid, _ in CURVE]
        lines.append({"label": label, "date": d, "y": vals})
        parts.append(f"{label} ({d}): " + ", ".join(f"{m} {v:.2f}%" for (_, m), v in zip(CURVE, vals)))
    chart = {"label": "Treasury yield curve (3M / 2Y / 10Y)", "group": "rates", "unit": "Percent", "kind": "curve",
             "maturities": [m for _, m in CURVE], "lines": lines}
    fact = {"id": "U:YIELD_CURVE", "type": "macro_update", "text": "Treasury yields. " + "; ".join(parts) + ".",
            "_source": {"label": "FRED DGS3MO, DGS2, DGS10: Treasury yields", "date": last,
                        "url": "https://fred.stlouisfed.org/series/DGS10"}}
    return chart, fact


# ---------- fact sheets ----------

def month_fact(p: dict, bench: dict, ticker: str) -> dict | None:
    rows = list(zip(p["date"], p["close"], p["adj"], p["volume"]))
    if len(rows) < 25:
        return None
    last = rows[-1]
    base = _back(rows, last[0], 30)
    if not base:
        return None
    window = [r for r in rows if r[0] > base[0]]
    hi = max(window, key=lambda r: r[1])
    lo = min(window, key=lambda r: r[1])
    chg = (last[2] / base[2] - 1) * 100
    text = (f"{ticker} last month: close ${base[1]:,.2f} on {base[0]} to ${last[1]:,.2f} on {last[0]}"
            + ("" if p["final"] else " (session still open when fetched)")
            + f", a change of {chg:+.2f}%. Highest close in the month ${hi[1]:,.2f} on {hi[0]}, "
              f"lowest close ${lo[1]:,.2f} on {lo[0]}.")
    rel = []
    for sym, b in bench.items():
        brows = list(zip(b["date"], b["adj"]))
        bb, bl = _back(brows, last[0], 30), brows[-1] if brows else None
        if bb and bl:
            rel.append(f"{b['label']} {(bl[1] / bb[1] - 1) * 100:+.2f}%")
    if rel:
        text += " Over the same month: " + ", ".join(rel) + "."
    vols = [r[3] for r in rows if r[3]]
    if len(vols) >= 25:
        recent, before = sum(vols[-5:]) / 5, sum(vols[-25:-5]) / 20
        text += f" Average volume of the last 5 sessions: {recent / before:.2f}x the 20 sessions before."
    return {"id": f"P1M:{ticker}", "type": "price_month", "text": text,
            "_source": {"label": f"yfinance daily prices, {ticker} (last month)", "date": last[0],
                        "url": f"https://finance.yahoo.com/quote/{ticker}"}}


def update_fact(sid: str, info, hist: list[tuple[str, float]]) -> dict | None:
    if len(hist) < 2:
        return None
    how = SERIES_CHART.get(sid, {}).get("transform")
    (d1, v1), (d0, v0) = hist[-1], hist[-2]
    text = f"{info['label']} ({sid}), latest update: {_f(v1)} for {d1}; previous {_f(v0)} for {d0}"
    text += f" (change {v1 - v0:+.2f})." if abs(v1) < 1000 else f" (change {v1 - v0:+,.0f})."
    for label, days in (("One month earlier", 30), ("One year earlier", 365)):
        r = _back(hist, d1, days)
        if r and r[0] != d0:
            text += f" {label}: {_f(r[1])} for {r[0]}."
    pts = transform(hist, how)
    if how == "yoy" and len(pts) >= 2:
        text += f" Year-over-year change: {pts[-1][1]:+.2f}% (previous month {pts[-2][1]:+.2f}%)."
    elif how == "diff" and len(pts) >= 2:
        text += f" Monthly change: {pts[-1][1]:+,.0f} thousand (previous month {pts[-2][1]:+,.0f} thousand)."
    elif how == "qoq_ann" and len(pts) >= 2:
        text += f" Annualised quarterly change: {pts[-1][1]:+.2f}% (previous quarter {pts[-2][1]:+.2f}%)."
    elif how == "ma4" and pts[-1][2] is not None:
        text += f" 4-week average: {_f(pts[-1][2])}."
    return {"id": f"U:{sid}", "type": "macro_update", "text": text + f" Units: {info['units']}.",
            "_source": {"label": f"FRED {sid}: {info['label']}", "date": d1,
                        "url": f"https://fred.stlouisfed.org/series/{sid}"}}


def fed_items(conn, day: date) -> list[dict]:
    since = (day - timedelta(days=FED_DAYS)).isoformat()
    rows = conn.execute("""SELECT * FROM items WHERE source = 'federal_reserve'
                           AND (published_at >= ? OR id = (SELECT id FROM items WHERE item_type = 'fomc_statement'
                                                           ORDER BY published_at DESC LIMIT 1))
                           AND published_at <= ? ORDER BY published_at DESC""",
                        (since, f"{day.isoformat()}T23:59:59Z")).fetchall()
    out = []
    for r in rows:
        it = dict(r)
        it.update(extra=json.loads(it["extra"] or "{}"), tickers=[], label_tickers=[], importance="high",
                  also_reported_by=[])
        out.append(it)
    return out


def stock_sheets(conn, day: date, tickers: list[str], prices: dict, bench: dict) -> dict[str, list[dict]]:
    items = run_digest.select_items(conn, day, None, max_age=timedelta(days=STOCK_NEWS_DAYS))
    sheets = {}
    for t in tickers:
        news = [run_digest.item_fact(conn, it) for it in items if t in it["label_tickers"]][:MAX_STOCK_NEWS]
        facts = [f for f in (month_fact(prices[t], bench, t) if prices.get(t) else None,
                             ratings_fact(t, ratings(conn, t)), earnings_fact(t, next_earnings(conn, t, day))) if f]
        if facts:
            sheets[t] = news + facts
    return sheets


def macro_sheet(conn, day: date, infos: dict, curve_fact: dict | None) -> list[dict]:
    facts = []
    for sid, info in infos.items():
        f = update_fact(sid, info, series_history(conn, sid, day))
        if f:
            facts.append(f)
    if curve_fact:
        facts.append(curve_fact)
    for it in fed_items(conn, day):
        facts.append(item_fact(conn, it))
    news = run_digest.select_items(conn, day, None, max_age=timedelta(days=MACRO_NEWS_DAYS))
    facts += [item_fact(conn, it) for it in news if not it["label_tickers"]][:MAX_MACRO_NEWS]
    return facts


# ---------- Claude writes, Python checks (same checks as the digest) ----------

def public(title: str, facts: list[dict]) -> dict:
    return {"section": title, "facts": [{k: v for k, v in f.items() if not k.startswith("_")} for f in facts]}


def prompt_for(rules: str, sheet: dict, failed: list[dict] | None) -> str:
    p = rules + json.dumps(sheet, ensure_ascii=False, indent=1)
    if failed:
        listed = [{"index": n, "part": f["part"], "text": f["text"], "cites": f["cites"], "problem": f["problem"]}
                  for n, f in enumerate(failed)]
        p += ("\n\n## Fix these sentences\nThese sentences failed the checks. Rewrite each one so it passes the rules, "
              'or leave it out. Reply in the same JSON format with only the rewritten sentences, each with its "part" '
              'and "fixes": the index of the sentence it replaces.\n' + json.dumps(listed, ensure_ascii=False, indent=1))
    return p


def read_sentences(result) -> tuple[list, str | None]:
    if not result.ok:
        return [], f"claude call failed: {result.error}"
    try:
        s = extract_json(result.text).get("sentences")
        if not isinstance(s, list):
            raise ValueError('no "sentences" list')
        return s, None
    except Exception as exc:
        return [], f"unreadable reply: {exc}"


_SPLIT = re.compile(r"(?<=[.!?])\s+(?=[A-Z])")


def split_long(raws: list) -> list:
    """Haiku sometimes packs a whole paragraph into one 'sentence'. Split it; each piece keeps the citations
    and is checked on its own (a piece fails only for its own numbers)."""
    out = []
    for raw in raws:
        text = raw.get("text") if isinstance(raw, dict) else None
        if isinstance(text, str) and (len(text) > 300 or len(_SPLIT.split(text)) > 1):
            out += [{**raw, "text": piece} for piece in _SPLIT.split(text) if piece.strip()]
        else:
            out.append(raw)
    return out


def check_sentence(raw, facts_by_id: dict, parts: set[str]) -> tuple[dict | None, str | None]:
    if not isinstance(raw, dict):
        return None, "not an object"
    part = raw.get("part")
    if part not in parts:
        return {"part": part, "text": str(raw.get("text", ""))[:300], "cites": raw.get("cites") or []}, \
            f"unknown part {part!r}"
    b, reason = check_bullet({k: v for k, v in raw.items() if k != "part"}, facts_by_id)
    if b is None:
        return {"part": part, "text": str(raw.get("text", ""))[:300], "cites": []}, reason
    out = {"part": part, "text": b.text, "cites": b.cites}
    if b.fixes is not None:
        out["fixes"] = b.fixes
    return out, reason


def _log(conn, day: str, name: str, model: str, result, n: int, in_path: Path, out_path: Path, status: str, error):
    conn.execute(
        """INSERT INTO llm_runs (task, run_day, batch, model, effort, prompt_version, item_count, status, seconds,
                                 input_path, output_path, error, usage, started_at)
           VALUES ('dashboard', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (day, name, model, claude_client.EFFORT, PROMPT_VERSION, n, status, round(result.seconds, 1), str(in_path),
         str(out_path), error, json.dumps(result.usage) if result.usage else None, utc_now_z()))


async def _call_all(calls: list[tuple[str, dict]]):
    """(prompt, job) pairs; at most MAX_PARALLEL Claude processes at once. A job may add call options
    ("call": tools / effort / timeout / system_prompt, used by DEEP DIVE)."""
    sem = asyncio.Semaphore(claude_client.MAX_PARALLEL)

    async def one(p: str, j: dict):
        async with sem:
            return await claude_client.run_claude(p, model=j["model"], **j.get("call", {}))
    return await asyncio.gather(*(one(p, j) for p, j in calls))


def narrate(conn, day: date, jobs: list[dict], out_dir: Path) -> dict:
    """jobs: {name, model, rules, sheet, facts_by_id, parts} + optional "check" (default check_sentence), "split"
    (default True: split packed paragraphs) and "call" (extra run_claude options).
    Returns {name: {"kept": [...], "removed": [...], "error"}}."""
    res = {j["name"]: {"kept": [], "removed": [], "error": None} for j in jobs}
    for j in jobs:
        (out_dir / f"{j['name']}_input.json").write_text(json.dumps(j["sheet"], ensure_ascii=False, indent=1),
                                                         encoding="utf-8")
    pending: dict[str, list] = {}
    again: set[str] = set()
    for attempt in (1, 2):
        todo = jobs if attempt == 1 else [j for j in jobs if pending.get(j["name"]) or j["name"] in again]
        if not todo:
            break
        replies = asyncio.run(_call_all([(prompt_for(j["rules"], j["sheet"],
                                                     None if attempt == 1 or j["name"] in again else pending[j["name"]]),
                                          j) for j in todo]))
        for j, r in zip(todo, replies):
            name, suffix = j["name"], "" if attempt == 1 else "_retry"
            sentences, error = read_sentences(r)
            out_path = out_dir / f"{name}_output{suffix}.json"
            out_path.write_text(json.dumps({"sentences": sentences} if not error else {"error": error, "raw": r.text},
                                           ensure_ascii=False, indent=1), encoding="utf-8")
            failed, fixed = [], set()
            for raw in (split_long(sentences) if j.get("split", True) else sentences):
                s, reason = j.get("check", check_sentence)(raw, j["facts_by_id"], j["parts"])
                if s is not None and reason is None:
                    res[name]["kept"].append({**s, "attempt": attempt})
                    if s.get("fixes") is not None:
                        fixed.add(s["fixes"])
                elif s is not None:
                    failed.append({**s, "problem": reason})
            if attempt == 1:
                (again.add(name) if error else pending.__setitem__(name, failed))
            elif name in again:
                res[name]["error"] = error
                res[name]["removed"] += failed
            else:
                why = {f.get("fixes"): f["problem"] for f in failed if f.get("fixes") is not None}
                res[name]["removed"] += [{**o, "problem": f"{o['problem']}; then {why.get(n, 'not fixed in the retry')}"}
                                         for n, o in enumerate(pending[name]) if n not in fixed]
            _log(conn, day.isoformat(), f"{name}{suffix}", j["model"], r, len(j["sheet"]["facts"]),
                 out_dir / f"{name}_input.json", out_path, "failed" if error else ("partial" if failed else "ok"),
                 error or ("; ".join(f["problem"] for f in failed[:3]) or None))
        conn.commit()
    return res


# ---------- news table from the checked digest ----------

def news_rows(digest: dict, importance: dict[str, str]) -> list[dict]:
    """One row per kept digest bullet. importance: fact ID -> M5 importance (news), filings / Fed count as high."""
    by_id = {v["id"]: v for v in digest.get("sources", {}).values()}
    rows = []
    for section, r in digest.get("sections", {}).items():
        for b in r.get("kept", []):
            srcs = [by_id[c] for c in b["cites"] if c in by_id]
            items = [s for s in srcs if s.get("title") or ":" in s.get("label", "")]
            titles = [s.get("title") or s["label"].split(": ", 1)[-1] for s in items if s["id"][0] in "NSF"]
            levels = [importance.get(c) or ("high" if c[0] in "SF" else None) for c in b["cites"]]
            rows.append({"ticker": section, "date": max((s.get("date") or "" for s in srcs), default=""),
                         "title": " / ".join(dict.fromkeys(titles))
                                  or "Data: " + ", ".join(s["label"].split(",")[0] for s in srcs),
                         "summary": b["text"],
                         "importance": "high" if "high" in levels else ("medium" if "medium" in levels else None),
                         "links": [{"label": s.get("publisher") or s["label"].split(":")[0], "url": s["url"]}
                                   for s in srcs if s.get("url")]})
    for section, heads in digest.get("headlines", {}).items():      # RSS headlines: no summary, link only
        for h in heads:
            rows.append({"ticker": section, "date": (h.get("published_at") or "")[:10], "title": h["title"],
                         "summary": "", "importance": h.get("importance"),
                         "links": [{"label": h.get("publisher") or "link", "url": h["url"]}] if h.get("url") else []})
    return rows


# ---------- build ----------

def run(day: date, *, use_claude: bool = True, refresh_fin: set[str] | None = None,
        refresh_deep: set[str] | None = None) -> dict:
    conn = connect()
    watch = load_watchlist_entries()
    tickers = [w["ticker"] for w in watch]
    names = {w["ticker"]: w.get("name") or w["ticker"] for w in watch}
    out_dir = DASH_DIR / day.isoformat()
    out_dir.mkdir(parents=True, exist_ok=True)

    digest_path = run_digest.DIGEST_DIR / day.isoformat() / "digest.json"
    if not digest_path.exists():
        raise SystemExit(f"No digest for {day}: run `python -m pipeline.run_digest --day {day}` first.")
    digest = json.loads(digest_path.read_text(encoding="utf-8"))
    news = news_rows(digest, {f"N{r['item_id']}": r["importance"] for r in conn.execute(
        "SELECT item_id, importance FROM item_labels WHERE prompt_version = ?", (run_digest.CLASSIFY_VERSION,))})

    prices = {t: price_history(conn, t, day) for t in tickers}
    bench = {}
    for sym, label in BENCHMARKS.items():
        h = price_history(conn, sym, day)
        if h:
            bench[sym] = {"label": label, "date": h["date"], "adj": h["adj"]}

    infos = {r["series_id"]: r for r in conn.execute("SELECT * FROM series_info")}
    order = [s for s in SERIES_CHART if s in infos] + [s for s in infos if s not in SERIES_CHART]
    series = {}
    for sid in order:
        c = series_chart(conn, sid, infos[sid], day)
        if c:
            series[sid] = c
    curve, curve_f = curve_chart(conn, day)
    if curve:
        series = {**{k: v for k, v in series.items() if k in ("DFEDTARU", "DFF")}, "YIELD_CURVE": curve,
                  **{k: v for k, v in series.items() if k not in ("DFEDTARU", "DFF")}}

    narratives = {"stocks": {}, "macro": {"big_picture": [], "series": {}}}
    removed, sources, status = [], {}, "ok"
    saved = out_dir / "dashboard.json"
    if not use_claude and saved.exists():     # rebuild charts / layout, keep the narratives already written today
        old = json.loads(saved.read_text(encoding="utf-8"))
        narratives, sources, removed = old["narratives"], old["sources"], old.get("removed", [])

    # Financials screen: statements every day (from the database), analysis once per filing (cache)
    fin, fin_narr, fin_jobs = {}, {}, []
    for t in tickers:
        stm = F.statements(conn, t)
        if not stm:
            continue
        fin[t] = stm
        acc = stm["latest"]["accession"] if stm.get("latest") else None
        cached = F.load_cache(t, acc, FIN_PROMPT_VERSION) if acc and t not in (refresh_fin or set()) else None
        if cached:
            fin_narr[t] = cached
        elif use_claude and acc:
            facts = F.fin_facts(conn, t, stm)
            fin_jobs.append({"name": f"FIN_{t}", "model": FIN_MODEL, "accession": acc,
                             "rules": (PROMPTS / "dashboard_fin.md").read_text("utf-8").format(),
                             "sheet": public(f"{t} ({names[t]}) financial statements", facts),
                             "facts_by_id": {x["id"]: x for x in facts}, "parts": {"overall", "IS", "BS", "CF"}})
    # DEEP DIVE: weekly web research per company (see pipeline/deep_dive.py); latest saved one shown otherwise
    deep, deep_jobs = {}, []
    for t in fin:
        c = DD.latest_cache(t)
        if c:
            deep[t] = c[1]
        if use_claude and DD.needs_refresh(t, day, force=t in (refresh_deep or set())):
            facts = DD.deep_facts(conn, t, fin[t], day, run_digest.CLASSIFY_VERSION)
            deep_jobs.append(DD.job(t, names[t], facts, day, public(f"{t} ({names[t]}) deep dive", facts)))

    if use_claude:
        s_sheets = stock_sheets(conn, day, tickers, prices, bench)
        m_facts = macro_sheet(conn, day, infos, curve_f)
        jobs = [{"name": t, "model": STOCK_MODEL, "rules": (PROMPTS / "dashboard_stock.md").read_text("utf-8").format(),
                 "sheet": public(f"{t} ({names[t]})", f), "facts_by_id": {x["id"]: x for x in f}, "parts": {"stock"}}
                for t, f in s_sheets.items()]
        jobs.append({"name": "MACRO", "model": MACRO_MODEL,
                     "rules": (PROMPTS / "dashboard_macro.md").read_text("utf-8").format(),
                     "sheet": public("Fed and macro", m_facts), "facts_by_id": {x["id"]: x for x in m_facts},
                     "parts": {"big_picture"} | {f["id"][2:] for f in m_facts if f["id"].startswith("U:")}})
        jobs += fin_jobs + deep_jobs
        for j in jobs:
            sources.update({fid: f["_source"] for fid, f in j["facts_by_id"].items()
                            if not j["name"].startswith(("FIN_", "DEEP_"))})
        results = narrate(conn, day, jobs, out_dir)
        for name, r in results.items():
            if name.startswith("DEEP_"):           # saved weekly, kept apart from the daily narratives
                t = name[5:]
                entry = DD.entry_from(t, r, next(j for j in deep_jobs if j["name"] == name), day)
                if r["error"] or not r["kept"]:
                    status = "partial"             # keep showing last week's deep dive
                else:
                    DD.save_cache(t, day, entry)
                    deep[t] = entry
                continue
            if name.startswith("FIN_"):            # cached per filing, kept apart from the daily narratives
                t = name[4:]
                narr = {}
                for s in r["kept"]:
                    narr.setdefault(s["part"], []).append({"text": s["text"], "cites": s["cites"]})
                job = next(j for j in fin_jobs if j["name"] == name)
                srcs = {f"{t}|{fid}": f["_source"] for fid, f in job["facts_by_id"].items()}
                entry = {"accession": job["accession"], "built_at": utc_now_z(), "narr": narr, "sources": srcs,
                         "removed": r["removed"], "error": r["error"]}
                if not r["error"]:
                    F.save_cache(t, job["accession"], FIN_PROMPT_VERSION, entry)
                else:
                    status = "partial"
                fin_narr[t] = entry
                continue
            removed += [{"section": name, **x} for x in r["removed"]]
            if r["error"]:
                status = "partial"
            for s in r["kept"]:
                if name == "MACRO":
                    target = narratives["macro"]["big_picture"] if s["part"] == "big_picture" else \
                        narratives["macro"]["series"].setdefault(s["part"], [])
                else:
                    target = narratives["stocks"].setdefault(name, [])
                target.append({"text": s["text"], "cites": s["cites"]})

    data = {"day": day.isoformat(), "built_at": utc_now_z(), "tickers": tickers, "names": names,
            "prices": prices, "benchmarks": bench, "series": series, "series_order": list(series),
            "news": news, "narratives": narratives, "sources": sources, "removed": removed,
            "fin": fin, "fin_narr": fin_narr, "deep": deep,
            "freshness": freshness_rows(conn, utc_now_z())}
    from plotly.offline import get_plotlyjs
    html = (TEMPLATE.read_text(encoding="utf-8")
            .replace("__DAY__", day.isoformat())
            .replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
            .replace("__PLOTLY__", get_plotlyjs()))
    path = out_dir / "dashboard.html"
    path.write_text(html, encoding="utf-8")
    (out_dir / "dashboard.json").write_text(json.dumps({k: v for k, v in data.items()
                                                        if k not in ("prices", "benchmarks", "series", "fin")},
                                                       ensure_ascii=False, indent=1), encoding="utf-8")
    kept = len(narratives["macro"]["big_picture"]) + sum(len(v) for v in narratives["macro"]["series"].values()) + \
        sum(len(v) for v in narratives["stocks"].values())
    old = conn.execute("SELECT id FROM dashboards WHERE day = ?", (day.isoformat(),)).fetchone()
    if old:
        conn.execute("DELETE FROM dashboards WHERE id = ?", (old["id"],))
    conn.execute("""INSERT INTO dashboards (day, status, built_at, news_rows, sentences_kept, sentences_removed, path)
                    VALUES (?, ?, ?, ?, ?, ?, ?)""", (day.isoformat(), status, data["built_at"], len(news), kept,
                                                       len(removed), str(path)))
    conn.commit()
    conn.close()
    return {"status": status, "path": str(path), "news": len(news), "kept": kept, "removed": removed,
            "fin_written": len(fin_jobs), "fin_cached": len(fin_narr) - len(fin_jobs), "deep_written": len(deep_jobs),
            "size_mb": round(path.stat().st_size / 1e6, 1)}


def load_watchlist_entries_tickers() -> list[str]:
    return [w["ticker"] for w in load_watchlist_entries()]


def main() -> None:
    parser = argparse.ArgumentParser(description="Build the daily dashboard")
    parser.add_argument("--day", default="today")
    parser.add_argument("--refresh-fin", nargs="*", metavar="TICKER",
                        help="Rewrite the financial analysis even if one exists for the latest filing (no tickers = all)")
    parser.add_argument("--refresh-deep", nargs="*", metavar="TICKER",
                        help="Run the DEEP DIVE web research now even if this week's exists (no tickers = all)")
    parser.add_argument("--no-claude", action="store_true",
                        help="No Claude calls: rebuild charts / news table, reuse today's saved narratives if any")
    args = parser.parse_args()
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)
    print(f"Dashboard {day} ({PROMPT_VERSION}; stocks {STOCK_MODEL}, macro {MACRO_MODEL})", flush=True)
    refresh = None if args.refresh_fin is None else (set(args.refresh_fin) or set(load_watchlist_entries_tickers()))
    deep = None if args.refresh_deep is None else (set(args.refresh_deep) or set(load_watchlist_entries_tickers()))
    s = run(day, use_claude=not args.no_claude, refresh_fin=refresh, refresh_deep=deep)
    print(f"  Status: {s['status']}  news rows: {s['news']}  sentences kept: {s['kept']}  removed: {len(s['removed'])}")
    for r in s["removed"]:
        print(f"    removed [{r['section']}/{r['part']}] {r['text'][:90]} -- {r['problem'][:120]}")
    print(f"  Financial analyses: {s['fin_written']} written (new filing), {s['fin_cached']} reused from cache")
    print(f"  Deep dives researched: {s['deep_written']} (weekly, Mondays; others reuse the latest)")
    print(f"  Dashboard: {s['path']}  ({s['size_mb']} MB)")


if __name__ == "__main__":
    main()
