"""Financials screen (Milestone 6b): the three statements from SEC company facts + what Claude reads about them.

Numbers come from the `financials` table exactly as reported (latest filed value per period if restated):
  - Income statement: 3-month quarters and full years. Q4 exists only inside the annual figure: shown as "—".
  - Balance sheet: point-in-time values at every quarter end.
  - Cash flow: as reported. 10-Qs report it year-to-date, so quarterly columns are labelled 3M / 6M / 9M / 12M.
  - A small set of ratios is computed by Python and clearly marked "computed".
Claude's analysis is written once per filing (cached in data/fundamentals/<TICKER>/) and reused until the next one.
"""
from __future__ import annotations

import json
from collections import defaultdict
from datetime import date

from connectors.base import PROJECT_ROOT

FUND_DIR = PROJECT_ROOT / "data" / "fundamentals"
MDA_CHARS = 45_000
RISK_CHARS = 45_000
QUARTERS_KEPT = 12
YEARS_KEPT = 6
TABLE_QUARTERS_FOR_CLAUDE = 6
TABLE_YEARS_FOR_CLAUDE = 4

# (key, label, kind, unit, concepts in order of preference). kind: flow (duration) | stock (instant)
STATEMENTS = {
    "IS": [
        ("revenue", "Revenue", "flow", "USD", ["Revenues", "RevenueFromContractWithCustomerExcludingAssessedTax",
                                              "RevenueFromContractWithCustomerIncludingAssessedTax"]),
        ("cogs", "Cost of revenue", "flow", "USD", ["CostOfRevenue", "CostOfGoodsAndServicesSold", "CostOfGoodsSold"]),
        ("gross_profit", "Gross profit", "flow", "USD", ["GrossProfit"]),
        ("rnd", "Research & development", "flow", "USD", ["ResearchAndDevelopmentExpense",
                                                          "ResearchAndDevelopmentExpenseExcludingAcquiredInProcessCost"]),
        ("sga", "Selling, general & admin", "flow", "USD", ["SellingGeneralAndAdministrativeExpense"]),
        ("opex", "Total operating expenses", "flow", "USD", ["OperatingExpenses"]),
        ("op_income", "Operating income", "flow", "USD", ["OperatingIncomeLoss"]),
        ("pretax", "Income before tax", "flow", "USD", [
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
            "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments"]),
        ("tax", "Income tax", "flow", "USD", ["IncomeTaxExpenseBenefit"]),
        ("net_income", "Net income", "flow", "USD", ["NetIncomeLoss"]),
        ("eps", "Diluted EPS", "flow", "USD/shares", ["EarningsPerShareDiluted"]),
        ("shares", "Diluted shares", "flow", "shares", ["WeightedAverageNumberOfDilutedSharesOutstanding"]),
    ],
    "BS": [
        ("cash", "Cash & equivalents", "stock", "USD", ["CashAndCashEquivalentsAtCarryingValue"]),
        ("sti", "Short-term investments", "stock", "USD", ["ShortTermInvestments", "MarketableSecuritiesCurrent",
                                                            "AvailableForSaleSecuritiesDebtSecuritiesCurrent"]),
        ("receivables", "Accounts receivable", "stock", "USD", ["AccountsReceivableNetCurrent"]),
        ("inventory", "Inventory", "stock", "USD", ["InventoryNet"]),
        ("current_assets", "Total current assets", "stock", "USD", ["AssetsCurrent"]),
        ("ppe", "Property & equipment", "stock", "USD", ["PropertyPlantAndEquipmentNet"]),
        ("goodwill", "Goodwill", "stock", "USD", ["Goodwill"]),
        ("assets", "Total assets", "stock", "USD", ["Assets"]),
        ("current_liabilities", "Total current liabilities", "stock", "USD", ["LiabilitiesCurrent"]),
        ("lt_debt", "Long-term debt", "stock", "USD", ["LongTermDebtNoncurrent", "ConvertibleDebtNoncurrent"]),
        ("liabilities", "Total liabilities", "stock", "USD", ["Liabilities"]),
        ("equity", "Shareholders' equity", "stock", "USD", ["StockholdersEquity"]),
    ],
    "CF": [
        ("cfo", "Operating cash flow", "flow", "USD", ["NetCashProvidedByUsedInOperatingActivities"]),
        ("capex", "Capital expenditure", "flow", "USD", ["PaymentsToAcquirePropertyPlantAndEquipment",
                                                         "PaymentsToAcquireProductiveAssets"]),
        ("cfi", "Investing cash flow", "flow", "USD", ["NetCashProvidedByUsedInInvestingActivities"]),
        ("cff", "Financing cash flow", "flow", "USD", ["NetCashProvidedByUsedInFinancingActivities"]),
        ("buybacks", "Share repurchases", "flow", "USD", ["PaymentsForRepurchaseOfCommonStock"]),
        ("dividends", "Dividends paid", "flow", "USD", ["PaymentsOfDividends", "PaymentsOfDividendsCommonStock"]),
        ("sbc", "Stock-based compensation", "flow", "USD", ["ShareBasedCompensation",
                                                            "AllocatedShareBasedCompensationExpense"]),
    ],
}
NAMES = {"IS": "Income statement", "BS": "Balance sheet", "CF": "Cash flow statement"}


def _days(start: str, end: str) -> int:
    return (date.fromisoformat(end) - date.fromisoformat(start)).days


def _facts(conn, ticker: str) -> dict:
    """{(concept, unit): {(start, end): (value, accession, filed)}} keeping the latest filed value per period."""
    out: dict = defaultdict(dict)
    for r in conn.execute("""SELECT concept, unit, period_start, period_end, value, accession_no, filed_date
                             FROM financials WHERE ticker = ? AND taxonomy = 'us-gaap'
                             ORDER BY filed_date""", (ticker,)):
        out[(r["concept"], r["unit"])][(r["period_start"], r["period_end"])] = (r["value"], r["accession_no"],
                                                                                 r["filed_date"])
    return out


def _period_labels(conn, ticker: str) -> dict[str, str]:
    """period_end -> fiscal label, taken from the filing that reported that period as its current period."""
    # a filing's own period = the period end that appears most often among its facts
    rows = conn.execute("""SELECT accession_no, period_end, fiscal_year fy, fiscal_period fp, COUNT(*) n
                           FROM financials WHERE ticker = ? AND form IN ('10-Q', '10-K')
                           GROUP BY accession_no, period_end""", (ticker,)).fetchall()
    best: dict = {}
    for r in rows:
        if r["fy"] and r["fp"] and (r["accession_no"] not in best or r["n"] > best[r["accession_no"]]["n"]):
            best[r["accession_no"]] = r
    labels = {}
    for r in sorted(best.values(), key=lambda x: x["accession_no"]):
        labels.setdefault(r["period_end"], f"FY{r['fy']}" if r["fp"] == "FY" else f"{r['fp']} FY{r['fy']}")
    return labels


def _filing_url(cik: int, accession: str) -> str:
    return f"https://www.sec.gov/Archives/edgar/data/{cik}/{accession.replace('-', '')}/{accession}-index.html"


def statements(conn, ticker: str) -> dict | None:
    facts = _facts(conn, ticker)
    if not facts:
        return None
    cik = conn.execute("SELECT cik FROM financials WHERE ticker = ? LIMIT 1", (ticker,)).fetchone()["cik"]
    labels = _period_labels(conn, ticker)
    fy_ends = sorted({e for e, l in labels.items() if l.startswith("FY")})
    # quarter-end columns = dates the balance sheet was reported at
    q_ends = sorted({e for (c, u), per in facts.items() if c == "Assets" for (s, e) in per if s == ""})[-QUARTERS_KEPT:]
    y_ends = fy_ends[-YEARS_KEPT:]

    def pick(concepts, unit, ok, ends):
        """The tag that covers the most of the shown periods (companies switch tags over the years)."""
        best, best_n = (None, {}), 0
        for c in concepts:
            per = facts.get((c, unit)) or {}
            n = sum(1 for e in ends if any(ee == e and ok(s, ee) for (s, ee) in per))
            if n > best_n:
                best, best_n = (c, per), n
        return best

    def col_label(e: str, annual: bool) -> str:
        lab = labels.get(e, e)
        return f"Q4 {lab}" if not annual and lab.startswith("FY") else lab

    def build(ends: list[str], annual: bool) -> dict:
        out = {"columns": [{"end": e, "label": col_label(e, annual)} for e in ends], "IS": [], "BS": [], "CF": []}
        for st, lines in STATEMENTS.items():
            for key, label, kind, unit, concepts in lines:
                if kind == "stock":
                    ok = lambda s, e: s == ""
                elif annual or st == "IS":
                    ok = (lambda s, e: s != "" and 350 <= _days(s, e) <= 380) if annual else \
                         (lambda s, e: s != "" and 80 <= _days(s, e) <= 100)
                else:                                    # quarterly cash flow: year-to-date as reported
                    ok = lambda s, e: s != "" and 80 <= _days(s, e) <= 380
                concept, per = pick(concepts, unit, ok, ends)
                vals, srcs, notes = [], [], []
                for e in ends:
                    cands = [(s, v) for (s, ee), v in per.items() if ee == e and ok(s, ee)]
                    if st == "CF" and not annual and cands:
                        s, v = max(cands, key=lambda x: _days(x[0], e))   # the longest = year-to-date
                        notes.append(f"{round(_days(s, e) / 30.4)}M")
                    elif cands:
                        s, v = cands[0]
                        notes.append("")
                    else:
                        v, s = None, None
                        q4 = st == "IS" and not annual and e in fy_ends
                        notes.append("Q4: see annual" if q4 else "")
                    vals.append(v[0] if v else None)
                    srcs.append(_filing_url(cik, v[1]) if v else None)
                out[st].append({"key": key, "label": label, "unit": unit, "concept": concept,
                                "values": vals, "src": srcs, "notes": notes})
        out["ratios"] = ratios(out)
        return out

    latest = conn.execute("""SELECT accession_no, form, filed_date, period_end FROM financials
                             WHERE ticker = ? AND form IN ('10-Q', '10-K') ORDER BY filed_date DESC, period_end DESC
                             LIMIT 1""", (ticker,)).fetchone()
    return {"quarterly": build(q_ends, False), "annual": build(y_ends, True),
            "latest": {"accession": latest["accession_no"], "form": latest["form"], "filed": latest["filed_date"],
                       "url": _filing_url(cik, latest["accession_no"])} if latest else None}


def ratios(t: dict) -> list[dict]:
    """A few ratios computed by Python from the reported lines (marked 'computed' on screen)."""
    get = lambda st, k: next((r["values"] for r in t[st] if r["key"] == k), [None] * len(t["columns"]))
    div = lambda a, b: [round(x / y * 100, 1) if x is not None and y not in (None, 0) else None for x, y in zip(a, b)]
    rev = get("IS", "revenue")
    cfo, capex = get("CF", "cfo"), get("CF", "capex")
    cf_notes = next((r["notes"] for r in t["CF"] if r["key"] == "cfo"), [""] * len(rev))
    rows = [
        ("gross_margin", "Gross margin %", div(get("IS", "gross_profit"), rev)),
        ("op_margin", "Operating margin %", div(get("IS", "op_income"), rev)),
        ("net_margin", "Net margin %", div(get("IS", "net_income"), rev)),
        ("fcf", "Free cash flow (op. CF − capex)",
         [a - b if a is not None and b is not None else None for a, b in zip(cfo, capex)]),
        ("current_ratio", "Current ratio (x)",
         [round(a / b, 2) if a is not None and b else None
          for a, b in zip(get("BS", "current_assets"), get("BS", "current_liabilities"))]),
        ("debt_equity", "Long-term debt / equity (x)",
         [round(a / b, 2) if a is not None and b else None for a, b in zip(get("BS", "lt_debt"), get("BS", "equity"))]),
    ]
    return [{"key": k, "label": l, "unit": "%" if l.endswith("%") else ("x" if "(x)" in l else "USD"),
             "values": v, "notes": cf_notes if k == "fcf" else [""] * len(v), "computed": True} for k, l, v in rows]


# ---------- what Claude reads ----------

def fmt(v, unit: str) -> str:
    if v is None:
        return "n/a"
    if unit == "USD/shares":
        return f"{v:.2f}"
    if unit == "shares":
        return f"{v / 1e6:,.1f}M"
    if unit in ("%",):
        return f"{v:.1f}%"
    if unit == "x":
        return f"{v:.2f}x"
    return f"{v / 1e6:,.1f}M"


def statement_text(stm: dict, st: str) -> str:
    parts = [f"{NAMES.get(st, 'Ratios computed by Python from the reported lines')} (USD millions unless stated)."]
    for period, n in (("annual", TABLE_YEARS_FOR_CLAUDE), ("quarterly", TABLE_QUARTERS_FOR_CLAUDE)):
        t = stm[period]
        cols = t["columns"][-n:]
        rows = t[st] if st != "RATIOS" else t["ratios"]
        lines = []
        for r in rows:
            vals = r["values"][-n:]
            notes = r["notes"][-n:]
            if all(v is None for v in vals):
                continue
            cells = [f"{c['label']} ({c['end']}{', ' + nt if nt and not nt.startswith('Q4') else ''}): {fmt(v, r['unit'])}"
                     for c, v, nt in zip(cols, vals, notes) if v is not None]
            lines.append(f"{r['label']}: " + "; ".join(cells))
        label = "Annual" if period == "annual" else ("Quarterly" + (" (cash flow is year-to-date as reported)"
                                                                    if st == "CF" else ""))
        parts.append(f"{label}:\n" + "\n".join(lines))
    return "\n".join(parts)


def filing_texts(conn, ticker: str) -> list[dict]:
    """Latest 10-Q / 10-K MD&A and the latest 10-K risk factors (+ the latest 10-Q risk-factor update)."""
    out = []
    rows = conn.execute("""SELECT f.accession_no, f.form, f.filed_date, f.index_url, i.document_id FROM filings f
                           JOIN items i ON i.id = f.item_id
                           WHERE (',' || f.tickers || ',') LIKE ? AND f.form IN ('10-Q', '10-K')
                           ORDER BY f.filed_date DESC""", (f"%,{ticker},%",)).fetchall()
    latest = rows[0] if rows else None
    latest_k = next((r for r in rows if r["form"] == "10-K"), None)

    def section(doc_id, name, limit):
        text = "\n".join(r["content"] for r in conn.execute(
            "SELECT content FROM document_sections WHERE document_id = ? AND section = ? ORDER BY seq", (doc_id, name)))
        return text[:limit]

    if latest:
        name = "Part I Item 2" if latest["form"] == "10-Q" else "Item 7"
        text = section(latest["document_id"], name, MDA_CHARS)
        if text:
            out.append({"id": "MDA", "type": "filing_mdna",
                        "text": f"Management's discussion and analysis from the {latest['form']} filed "
                                f"{latest['filed_date']}:\n{text}",
                        "_source": {"label": f"SEC {latest['form']} {ticker}: MD&A", "date": latest["filed_date"],
                                    "url": latest["index_url"]}})
        if latest["form"] == "10-Q":
            upd = section(latest["document_id"], "Part II Item 1A", 8000)
            if upd:
                out.append({"id": "RISKQ", "type": "filing_risk",
                            "text": f"Risk factor update in the 10-Q filed {latest['filed_date']}:\n{upd}",
                            "_source": {"label": f"SEC 10-Q {ticker}: risk factor update",
                                        "date": latest["filed_date"], "url": latest["index_url"]}})
    if latest_k:
        text = section(latest_k["document_id"], "Item 1A", RISK_CHARS)
        if text:
            out.append({"id": "RISK", "type": "filing_risk",
                        "text": f"Risk factors from the 10-K filed {latest_k['filed_date']}:\n{text}",
                        "_source": {"label": f"SEC 10-K {ticker}: risk factors", "date": latest_k["filed_date"],
                                    "url": latest_k["index_url"]}})
    return out


def fin_facts(conn, ticker: str, stm: dict) -> list[dict]:
    url = stm["latest"]["url"] if stm.get("latest") else None
    facts = [{"id": f"FS:{st}", "type": "statement", "text": statement_text(stm, st),
              "_source": {"label": f"SEC company facts {ticker}: {NAMES.get(st, 'computed ratios')}",
                          "date": stm["latest"]["filed"] if stm.get("latest") else "", "url": url}}
             for st in ("IS", "BS", "CF", "RATIOS")]
    return facts + filing_texts(conn, ticker)


def cache_path(ticker: str, accession: str, prompt_version: str):
    return FUND_DIR / ticker / f"{accession}_{prompt_version}.json"


def load_cache(ticker: str, accession: str, prompt_version: str) -> dict | None:
    p = cache_path(ticker, accession, prompt_version)
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def save_cache(ticker: str, accession: str, prompt_version: str, data: dict) -> None:
    p = cache_path(ticker, accession, prompt_version)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")
