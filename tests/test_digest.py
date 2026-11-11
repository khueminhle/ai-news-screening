"""Milestone 6: the checks, and the digest end to end with a fake Claude."""
import json
import sys
from collections import Counter
from datetime import date
from pathlib import Path

import pytest

from checks.advice_guard import check_advice
from checks.citation_check import check_citations
from checks.factcheck import check_numbers, numbers_in
from connectors.base import Item
from llm import claude_client
from pipeline import run_digest
from storage import db

FAKE = Path(__file__).with_name("fake_claude.py")


# ---------- check 1: numbers ----------

def test_numbers_ignore_dates_years_forms_and_index_names():
    text = "On Nov 6, 2026 (2026-11-06) the 10-Q for fiscal Q1 FY27 showed the S&P 500 and Nasdaq-100 and 10-year yield"
    assert numbers_in(text) == []
    assert numbers_in("Revenue $1,026.2 million, up +39.9%, EPS $5.29 [N82]") == ["1026.2", "39.9", "5.29"]


@pytest.mark.parametrize("bullet, ok", [
    ("Revenue was $1,026.2 million.", True),
    ("Revenue was $1.026 billion.", True),          # same digits, different unit word
    ("Shares rose 16.8% on the day.", True),
    ("Revenue was $1.03B.", False),                 # rounded: not as written
    ("Revenue was $1,300 million.", False),         # invented
])
def test_number_must_be_in_cited_text(bullet, ok):
    facts = ["Sales $1.026B; revenue $1,026.2 million", "1-day +16.8%"]
    assert (check_numbers(bullet, facts) is None) is ok


def test_number_from_an_uncited_fact_fails():
    assert check_numbers("EPS was $5.29.", ["Revenue $1,026.2 million"]) is not None


# ---------- citations and advice ----------

def test_citations():
    assert check_citations(["N1"], {"N1", "P:UI"}) is None
    assert "no citation" in check_citations([], {"N1"})
    assert "N9" in check_citations(["N1", "N9"], {"N1"})


@pytest.mark.parametrize("text, types, ok", [
    ("Investors should buy the stock.", {"news"}, False),
    ("The stock looks undervalued.", {"price"}, False),
    ("Oppenheimer upgraded AMD to Outperform, according to Benzinga.", {"news"}, True),
    ("Finnhub analyst counts: 1 buy, 3 hold.", {"analyst_counts"}, True),
    ("Analysts say buy, but the bullet cites only prices.", {"price"}, False),
    ("The board approved a $10B buyback after the selloff.", {"news"}, True),     # not advice words
    ("The Fed held rates steady.", {"fed_release"}, True),
    ("The Fed decided to hold rates at 4%.", {"macro"}, True),                   # hold = keep rates, not a rating
    ("Investments are carried at fair value.", {"statement"}, True),             # accounting term
    ("We rate the stock a hold.", {"price"}, False),
    ("The stock was downgraded to hold.", {"price"}, False),                     # rating, cites no news/counts
])
def test_advice_guard(text, types, ok):
    assert (check_advice(text, types) is None) is ok


# ---------- digest end to end ----------

@pytest.fixture
def setup(tmp_path, monkeypatch):
    monkeypatch.setattr(claude_client, "claude_command", lambda: [sys.executable, str(FAKE)])
    path = tmp_path / "t.db"
    monkeypatch.setattr(run_digest, "connect", lambda: db.connect(path))
    monkeypatch.setattr(run_digest, "DIGEST_DIR", tmp_path / "digests")
    conn = db.connect(path)

    def add(sid, title, summary="Sales $1.026B", ticker="UI", importance="high", relevant=1, near=None, kind="filing"):
        """kind="filing": an 8-K (goes to Claude's fact sheet); kind="news": an RSS headline (headline list only)."""
        if kind == "news":
            db.save_items(conn, [Item(source="news_rss", source_id=sid, tickers=[ticker], title=title,
                                      url="https://x/" + sid, published_at="2026-11-09T12:00:00Z", item_type="news",
                                      extra={"publisher": "Pub" + sid})])
        else:
            db._upsert_item(conn, Item(source="sec_edgar", source_id=sid, tickers=[ticker], title=title,
                                       url="https://x/" + sid, published_at="2026-11-09T12:00:00Z", item_type="8-K",
                                       extra={"company_name": ticker}), Counter(), summary=summary, publisher="Pub" + sid)
        item_id = conn.execute("SELECT id FROM items WHERE source_id = ?", (sid,)).fetchone()[0]
        conn.execute("""INSERT INTO item_labels (item_id, prompt_version, model, relevant, tickers, event_type,
                        importance, sentiment, near_duplicate_of, one_line, labelled_at)
                        VALUES (?, 'classify-v1', 'm', ?, ?, 'earnings', ?, 'positive', ?, 'x', 'x')""",
                     (item_id, relevant, json.dumps([ticker] if relevant else []), importance, near))
        conn.commit()
        return item_id
    yield conn, add
    conn.close()


def test_digest_keeps_good_fixes_retry_and_removes_bad(setup):
    conn, add = setup
    good = add("1", "Ubiquiti results")
    add("2", "Copy of the results", near=good)                       # copy: folded into the first story
    add("3", "INVENT story")
    add("4", "ALWAYSBAD story")
    add("5", "ADVICE story")
    add("6", "FAKECITE story", ticker="AAPL")
    add("7", "Low importance", importance="low")
    add("8", "Irrelevant", relevant=0)

    s = run_digest.run(date(2026, 11, 10))
    assert s["items"] == 5 and s["status"] == "ok"
    assert s["kept"] == 2 and s["retried"] == 1                       # good + INVENT fixed in the retry
    assert s["removed"] == 3                                          # ALWAYSBAD, ADVICE, FAKECITE

    md = Path(s["path"]).read_text(encoding="utf-8")
    assert md.count("SIMULATION, NOT INVESTMENT ADVICE") == 2
    assert "Also reported by: Pub2" in md
    assert "Low importance" not in md and "Irrelevant" not in md
    assert "$9.99B" not in md.split("## Removed by fact check")[0]   # the invented number never reaches the digest
    removed = md.split("## Removed by fact check")[1]
    assert "ALWAYSBAD" in removed and "ADVICE" in removed and "N999999" in removed

    lines = conn.execute("SELECT status, COUNT(*) FROM digest_lines GROUP BY status").fetchall()
    assert dict(lines) == {"kept": 2, "removed": 3}
    folder = Path(s["path"]).parent
    assert (folder / "UI_input.json").exists() and (folder / "UI_output_retry.json").exists()
    assert (folder / "factcheck_report.json").exists()


def test_next_digest_covers_only_new_items(setup):
    conn, add = setup
    add("1", "First story")
    conn.execute("UPDATE items SET first_seen_at = '2026-11-09T00:00:00Z'")
    conn.commit()
    assert run_digest.run(date(2026, 11, 10))["items"] == 1
    assert run_digest.run(date(2026, 11, 11))["items"] == 0           # nothing new since the previous digest
    add("2", "Second story")
    conn.execute("UPDATE items SET first_seen_at = '2099-01-01T00:00:00Z' WHERE source_id = '2'")
    conn.commit()
    assert run_digest.run(date(2026, 11, 11))["items"] == 1
    assert conn.execute("SELECT COUNT(*) FROM digests").fetchone()[0] == 2   # re-building a day replaces it


def test_news_is_a_headline_list_not_claude_bullets(setup):
    conn, add = setup
    add("1", "Ubiquiti results")                                       # filing -> Claude bullet
    add("2", "Ubiquiti wins big contract", kind="news", importance="high")
    add("3", "Ubiquiti product review", kind="news", importance="medium", ticker="AAPL")
    add("4", "Ubiquiti listicle", kind="news", importance="low")
    s = run_digest.run(date(2026, 11, 10))
    assert s["headlines"] == 2 and s["kept"] == 1
    md = Path(s["path"]).read_text(encoding="utf-8")
    assert "**HIGH** [Ubiquiti wins big contract](https://x/2) — Pub2" in md
    assert "### AAPL" in md and "**MEDIUM** [Ubiquiti product review]" in md     # a section with headlines only
    assert "Ubiquiti listicle" not in md
    folder = Path(s["path"]).parent
    assert not (folder / "AAPL_input.json").exists()                          # no Claude call for headline-only
    facts = json.loads((folder / "UI_input.json").read_text(encoding="utf-8"))["facts"]
    assert not any(f["id"].startswith("N") for f in facts)                    # news never reaches Claude's sheet
