"""Milestone 6b: dashboard data, narratives (checked like the digest) and the HTML file, with a fake Claude."""
import json
import sys
from datetime import date, timedelta
from pathlib import Path

import pytest

from llm import claude_client
from pipeline import run_dashboard, run_digest
from storage import db

FAKE = Path(__file__).with_name("fake_claude.py")


def test_transforms():
    monthly = [(f"2025-{m:02d}-01", 100.0 + m) for m in range(1, 13)] + [("2026-01-01", 113.0 * 1.03)]
    yoy = run_dashboard.transform(monthly, "yoy")
    assert len(yoy) == 1 and round(yoy[0][1], 2) == round((113.0 * 1.03 / 101.0 - 1) * 100, 2)
    assert run_dashboard.transform([("a", 100.0), ("b", 120.0)], "diff") == [("b", 20.0, None)]
    ma = run_dashboard.transform([(str(i), float(i)) for i in range(5)], "ma4")
    assert ma[2][2] is None and ma[3][2] == 1.5


def test_split_long_keeps_citations():
    out = run_dashboard.split_long([{"part": "stock", "text": "First point. Second point here.", "cites": ["P1M:UI"]}])
    assert [o["text"] for o in out] == ["First point.", "Second point here."]
    assert all(o["cites"] == ["P1M:UI"] for o in out)


@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_client, "claude_command", lambda: [sys.executable, str(FAKE)])
    path = tmp_path / "t.db"
    monkeypatch.setattr(run_dashboard, "connect", lambda: db.connect(path))
    monkeypatch.setattr(run_dashboard, "DASH_DIR", tmp_path / "dash")
    monkeypatch.setattr(run_digest, "DIGEST_DIR", tmp_path / "digests")
    conn = db.connect(path)
    start = date(2026, 9, 1)
    for sym in ("UI", "^GSPC", "^NDX"):
        for i in range(60):
            d = (start + timedelta(days=i)).isoformat()
            px = 100 + i
            conn.execute("""INSERT INTO prices (ticker, date, open, high, low, close, adj_close, volume, is_final,
                            source, fetched_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 1, 'yfinance', 'x')""",
                         (sym, d, px, px + 1, px - 1, px, px, 1000 + i))
    for sid, label in (("DGS10", "10-year Treasury yield"), ("CPIAUCSL", "Consumer Price Index (CPI)")):
        conn.execute("INSERT INTO series_info (series_id, label, grp, units) VALUES (?, ?, 'rates', 'Percent')",
                     (sid, label))
        for m in range(1, 25):
            conn.execute("INSERT INTO macro_series (series_id, date, value, fetched_at) VALUES (?, ?, ?, 'x')",
                         (sid, f"{2024 + (m - 1) // 12}-{(m - 1) % 12 + 1:02d}-01", 4.0 + m / 10))
    conn.execute("INSERT INTO fetch_runs (connector, run_day, started_at, finished_at, status) "
                 "VALUES ('yfinance', 'x', 'x', '2026-10-30T00:00:00Z', 'ok')")
    conn.commit()
    day_dir = tmp_path / "digests" / "2026-10-30"
    day_dir.mkdir(parents=True)
    (day_dir / "digest.json").write_text(json.dumps({
        "sections": {"UI": {"kept": [{"text": "Ubiquiti reported revenue of $1,026.2 million.", "cites": ["S5"]}],
                            "removed": [], "error": None}},
        "sources": {"1": {"id": "S5", "label": "SEC 8-K, Ubiquiti Inc.: Ubiquiti Inc. 8-K", "title": "Ubiquiti Inc. 8-K",
                          "publisher": "SEC 8-K, Ubiquiti Inc.", "date": "2026-10-29", "url": "https://sec.gov/x"}}}))
    yield conn
    conn.close()


def test_dashboard_builds_checked_narratives_and_html(setup):
    s = run_dashboard.run(date(2026, 10, 30))
    html = Path(s["path"]).read_text(encoding="utf-8")
    data = json.loads((Path(s["path"]).parent / "dashboard.json").read_text(encoding="utf-8"))

    assert s["news"] == 1 and "Ubiquiti reported revenue" in html and "https://sec.gov/x" in html
    assert "SIMULATION · NOT INVESTMENT ADVICE" in html and "Plotly" in html and "__DATA__" not in html
    stock = [x["text"] for x in data["narratives"]["stocks"]["UI"]]
    assert stock == ["The stock moved with the market.", "News explained part of it."]      # split, both kept
    assert any("99.99" in r["text"] for r in data["removed"])                                # invented number removed
    assert "99.99%" not in json.dumps(data["narratives"])
    assert data["narratives"]["macro"]["big_picture"]
    assert set(data["narratives"]["macro"]["series"]) == {"DGS10", "CPIAUCSL"}


def test_no_claude_rebuild_keeps_saved_narratives(setup):
    first = run_dashboard.run(date(2026, 10, 30))
    again = run_dashboard.run(date(2026, 10, 30), use_claude=False)
    assert again["kept"] == first["kept"] > 0


def _add_facts(conn):
    rows = [  # (concept, unit, start, end, value, accession, form, fy, fp, filed)
        ("Revenues", "USD", "2025-07-01", "2026-06-30", 4000e6, "A-10K", "10-K", 2026, "FY", "2026-08-20"),
        ("Revenues", "USD", "2025-07-01", "2025-09-30", 900e6, "A-Q1", "10-Q", 2026, "Q1", "2025-11-05"),
        ("Revenues", "USD", "2026-07-01", "2026-09-30", 1100e6, "A-Q1B", "10-Q", 2027, "Q1", "2026-11-06"),
        ("GrossProfit", "USD", "2026-07-01", "2026-09-30", 500e6, "A-Q1B", "10-Q", 2027, "Q1", "2026-11-06"),
        ("Assets", "USD", "", "2025-09-30", 9000e6, "A-Q1", "10-Q", 2026, "Q1", "2025-11-05"),
        ("Assets", "USD", "", "2026-06-30", 9500e6, "A-10K", "10-K", 2026, "FY", "2026-08-20"),
        ("Assets", "USD", "", "2026-09-30", 9900e6, "A-Q1B", "10-Q", 2027, "Q1", "2026-11-06"),
        ("NetCashProvidedByUsedInOperatingActivities", "USD", "2025-07-01", "2026-06-30", 1200e6, "A-10K", "10-K", 2026, "FY", "2026-08-20"),
        ("NetCashProvidedByUsedInOperatingActivities", "USD", "2026-07-01", "2026-09-30", 300e6, "A-Q1B", "10-Q", 2027, "Q1", "2026-11-06"),
    ]
    for c, u, s, e, v, a, f, fy, fp, filed in rows:
        conn.execute("""INSERT INTO financials (ticker, cik, taxonomy, concept, unit, period_start, period_end, value,
                        fiscal_year, fiscal_period, form, accession_no, filed_date, fetched_at)
                        VALUES ('UI', 1, 'us-gaap', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'x')""", (c, u, s, e, v, fy, fp, f, a, filed))
    conn.commit()


def test_statements_q4_ytd_and_ratios(setup):
    from pipeline import fundamentals as F
    _add_facts(setup)
    s = F.statements(setup, "UI")
    q = s["quarterly"]
    assert [c["label"] for c in q["columns"]] == ["Q1 FY2026", "Q4 FY2026", "Q1 FY2027"]
    rev = next(r for r in q["IS"] if r["key"] == "revenue")
    assert rev["values"] == [900e6, None, 1100e6] and rev["notes"][1] == "Q4: see annual"   # no Q4 maths
    cfo = next(r for r in q["CF"] if r["key"] == "cfo")
    assert cfo["notes"][1:] == ["12M", "3M"]                                                # year-to-date as filed
    gm = next(r for r in q["ratios"] if r["key"] == "gross_margin")
    assert gm["values"][-1] == 45.5 and gm["computed"]
    assert s["annual"]["columns"][-1]["label"] == "FY2026" and s["latest"]["accession"] == "A-Q1B"
    assert "1,100.0M" in F.statement_text(s, "IS")


def test_financial_analysis_is_cached_per_filing(setup, monkeypatch):
    from pipeline import fundamentals as F
    monkeypatch.setattr(F, "FUND_DIR", Path(run_dashboard.DASH_DIR).parent / "fund")
    _add_facts(setup)
    first = run_dashboard.run(date(2026, 10, 30))
    assert first["fin_written"] == 1
    data = json.loads(Path(first["path"]).read_text(encoding="utf-8").split("const D = ", 1)[1].split(";\nconst S", 1)[0])
    assert set(data["fin_narr"]["UI"]["narr"]) == {"overall", "IS", "BS", "CF"}
    again = run_dashboard.run(date(2026, 10, 30))                 # same filing -> reused, no new Claude call
    assert again["fin_written"] == 0 and again["fin_cached"] == 1


def test_missing_digest_stops_with_a_clear_message(setup):
    with pytest.raises(SystemExit, match="run_digest"):
        run_dashboard.run(date(2026, 10, 31))
