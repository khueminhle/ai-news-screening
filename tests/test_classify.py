"""Milestone 5: Claude runner, reply checks, classifier end to end (with a fake claude), company facts."""
import asyncio
import json
import sys
import time
from datetime import date
from pathlib import Path

import pytest

from connectors.base import Item
from connectors.sec_facts import iter_facts
from llm import claude_client
from llm.schemas import check_label, extract_json
from pipeline import run_classify
from storage import db

FAKE = Path(__file__).with_name("fake_claude.py")


@pytest.fixture
def fake_claude(monkeypatch):
    monkeypatch.setattr(claude_client, "claude_command", lambda: [sys.executable, str(FAKE)])


def label(**over):
    base = {"item_id": 1, "relevant": True, "tickers": ["UI"], "event_type": "earnings", "importance": "high",
            "importance_reason": "results", "sentiment": "positive", "fiscal_period": None,
            "figures": [{"label": "EPS", "value": "$5.32", "quote": "Adj. EPS $5.32 Beats"}],
            "near_duplicate_of": None, "one_line": "Ubiquiti beat estimates."}
    return {**base, **over}


CHECK = dict(batch_ids={1, 2}, known_ids={1, 2, 3}, watchlist={"UI", "AAPL"},
             source_text={1: "Ubiquiti Q1 Adj. EPS $5.32 Beats $4.57 Estimate"})


# ---------- reply checks ----------

def test_extract_json_strips_fences():
    assert extract_json('```json\n{"items": []}\n```') == {"items": []}
    assert extract_json('Here you go: {"items": [1]}') == {"items": [1]}
    with pytest.raises(ValueError):
        extract_json("no json here")


def test_good_label_passes():
    lab, reason = check_label(label(), **CHECK)
    assert reason is None and lab.item_id == 1


@pytest.mark.parametrize("over, expected", [
    ({"event_type": "rumour"}, "schema"),
    ({"tickers": ["NVDA"]}, "not on the watchlist"),
    ({"item_id": 99}, "unknown item_id"),
    ({"near_duplicate_of": 42}, "near_duplicate_of"),
    ({"near_duplicate_of": 1}, "near_duplicate_of"),                     # itself
    ({"figures": [{"label": "EPS", "value": "$5.40", "quote": "EPS $5.40"}]}, "quote not found"),
    ({"figures": [{"label": "EPS", "value": "$4.99", "quote": "Adj. EPS $5.32"}]}, "is not in its quote"),
    ({"extra_field": 1}, "schema"),
])
def test_bad_labels_are_rejected(over, expected):
    lab, reason = check_label(label(**over), **CHECK)
    assert lab is None and expected in reason


def test_quote_check_ignores_case_quotes_and_table_pipes():
    text = {1: "Revenues | 1,026.2 | 937.3\nUbiquiti’s record quarter"}
    figs = [{"label": "revenue", "value": "1,026.2", "quote": "revenues 1,026.2"}]
    lab, reason = check_label(label(figures=figs, one_line="Ubiquiti's record quarter"), **{**CHECK, "source_text": text})
    assert reason is None


# ---------- Claude runner ----------

def test_runner_reads_reply_from_the_pipe(fake_claude):
    res = asyncio.run(claude_client.run_claude("## Input\n\n" + json.dumps({"items": []})))
    assert res.ok and extract_json(res.text) == {"items": []} and res.usage["usage"]["output_tokens"] == 10


def test_runner_kills_claude_on_timeout(fake_claude):
    started = time.monotonic()
    res = asyncio.run(claude_client.run_claude("SLEEP", timeout=2))
    assert not res.ok and res.returncode == 124 and "TIMEOUT" in res.error
    assert time.monotonic() - started < 20


def test_runner_never_passes_an_api_key(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    env = claude_client.child_env()
    assert "ANTHROPIC_API_KEY" not in env and env["MAX_THINKING_TOKENS"] == "0"
    args = claude_client.build_args("m", "low")
    assert args[args.index("--tools") + 1] == ""                       # no tools for Claude


# ---------- classifier end to end ----------

@pytest.fixture
def setup(tmp_path, monkeypatch, fake_claude):
    path = tmp_path / "t.db"
    monkeypatch.setattr(run_classify, "connect", lambda: db.connect(path))
    monkeypatch.setattr(run_classify, "LLM_DIR", tmp_path / "llm")
    conn = db.connect(path)

    def add(sid, title, tickers=("UI",)):
        db.save_items(conn, [Item(source="finnhub", source_id=sid, tickers=list(tickers), title=title, url="u",
                                  published_at="2026-11-06T12:00:00Z", item_type="news", extra={})])
    yield conn, add, tmp_path
    conn.close()


def test_classify_saves_labels_and_files(setup):
    conn, add, tmp = setup
    for n in range(12):                                                 # 12 items -> 2 batches of 10 + 2
        add(str(n), f"Ubiquiti Q1 Adj. EPS $5.32 story {n}")
    s = run_classify.run(date(2026, 11, 10))
    assert s["labelled"] == 12 and s["quarantined"] == 0 and s["calls"] == 2
    assert conn.execute("SELECT COUNT(*) FROM item_labels").fetchone()[0] == 12
    assert conn.execute("SELECT COUNT(*) FROM item_figures").fetchone()[0] == 12
    assert conn.execute("SELECT COUNT(*) FROM items WHERE relevance = 'relevant'").fetchone()[0] == 12
    folder = Path(s["folder"])
    assert sorted(p.name for p in folder.iterdir()) == ["UI_batch01_input.json", "UI_batch01_output.json",
                                                         "UI_batch02_input.json", "UI_batch02_output.json"]
    again = run_classify.run(date(2026, 11, 10))                       # re-run: nothing left to do
    assert again["items"] == 0 and again["calls"] == 0


def test_bad_reply_is_retried_then_quarantined(setup):
    conn, add, tmp = setup
    add("1", "Ubiquiti Q1 Adj. EPS $5.32")
    add("2", "BADQUOTE Ubiquiti Q1 Adj. EPS $5.32")
    add("3", "GARBAGE", tickers=("AAPL",))
    s = run_classify.run(date(2026, 11, 10))
    assert s["labelled"] == 1 and s["quarantined"] == 2 and s["calls"] == 4   # 2 batches + 2 retries
    reasons = dict(conn.execute("SELECT i.title, q.reason FROM llm_quarantine q JOIN items i ON i.id = q.item_id"))
    assert "quote not found" in reasons["BADQUOTE Ubiquiti Q1 Adj. EPS $5.32"]
    assert "unreadable reply" in reasons["GARBAGE"]
    assert (Path(s["quarantine_folder"]) / "quarantined_items.json").exists()
    assert run_classify.run(date(2026, 11, 10))["items"] == 0           # quarantined items are not retried daily
    assert run_classify.run(date(2026, 11, 10), retry_quarantine=True)["items"] == 2


def test_backfilled_filings_are_not_classified(setup):
    conn, add, tmp = setup
    conn.execute("""INSERT INTO items (source, source_id, item_type, title, extra, first_seen_at, last_seen_at)
                    VALUES ('sec_edgar', 'a1', '10-Q', 'Old 10-Q', '{"is_backfill": true}', 'x', 'x')""")
    conn.commit()
    assert run_classify.pending_items(conn) == []


# ---------- company facts ----------

def test_company_facts_stored_as_reported(tmp_path):
    raw = {"facts": {"us-gaap": {
        "Revenues": {"label": "Revenues", "units": {"USD": [
            {"start": "2026-07-01", "end": "2026-09-30", "val": 1026248000, "accn": "A-26", "fy": 2027, "fp": "Q1",
             "form": "10-Q", "filed": "2026-11-06", "frame": "CY2026Q3"},
            {"start": "2025-07-01", "end": "2025-09-30", "val": 733773000, "accn": "A-26", "fy": 2027, "fp": "Q1",
             "form": "10-Q", "filed": "2026-11-06"}]}},
        "Assets": {"label": "Assets", "units": {"USD": [
            {"end": "2026-09-30", "val": 5, "accn": "A-26", "fy": 2027, "fp": "Q1", "form": "10-Q", "filed": "2026-11-06"}]}}}}}
    p = tmp_path / "CIK1.json"
    p.write_text(json.dumps(raw))
    assert len(list(iter_facts(raw))) == 3
    conn = db.connect(tmp_path / "f.db")
    it = Item(source="sec_facts", source_id="CIK1", tickers=["UI"], title="t", url="u", published_at=None,
              item_type="company_facts", extra={"cik": 1, "raw_path": str(p)})
    assert db.save_items(conn, [it])["financial_facts_added"] == 3
    assert db.save_items(conn, [it])["financial_facts_added"] == 0     # re-run: no duplicates
    assert conn.execute("SELECT period_start FROM financials WHERE concept = 'Assets'").fetchone()[0] == ""
    # last year's quarter keeps the FILING's fy/fp, exactly as SEC reports it
    assert conn.execute("SELECT fiscal_year FROM financials WHERE period_end = '2025-09-30'").fetchone()[0] == 2027
    conn.close()
