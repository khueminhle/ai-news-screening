"""Connector logic on saved real responses (no network, no API keys)."""
import json
from pathlib import Path

from bs4 import BeautifulSoup

from connectors.finnhub import FinnhubConnector
from connectors.newsdata import NewsDataConnector
from connectors.sec import columns_to_rows, skip_reason

FIXTURES = Path(__file__).parent / "fixtures"


def test_finnhub_news_picks_day_or_latest_earlier_day():
    c = object.__new__(FinnhubConnector)          # no API key needed for parsing
    c.fallback_to_latest = True
    c.name = "finnhub"
    stories = json.loads((FIXTURES / "finnhub_company_news_UI.json").read_text(encoding="utf-8"))
    items = c._pick_news("UI", stories, "2099-01-01")
    assert items and all(it.extra["is_latest_fallback"] for it in items)
    assert len({it.published_at[:10] for it in items}) == 1


def test_newsdata_google_copy_has_same_dedupe_key():
    a = {"title": "Apple is racing to make Siri more agentic", "source_id": "theverge"}
    b = {"title": "Apple is racing to make Siri more agentic - The Verge", "source_id": "google"}
    assert NewsDataConnector._dedupe_key(a) == NewsDataConnector._dedupe_key(b)


def test_newsdata_fixture_has_placeholders_we_must_drop():
    batch = json.loads((FIXTURES / "newsdata_market_batch.json").read_text(encoding="utf-8"))
    assert all(a["content"] == "ONLY AVAILABLE IN PAID PLANS" for a in batch["results"])


def test_sec_index_download_rules():
    soup = BeautifulSoup((FIXTURES / "sec_8k_index.html").read_bytes(), "html.parser")
    decisions = {}
    for tr in soup.select("table.tableFile tr")[1:]:
        tds, link = tr.find_all("td"), tr.find("a")
        if link and len(tds) >= 4:
            name, typ, desc = link.get_text(strip=True), tds[3].get_text(strip=True), tds[1].get_text(strip=True)
            decisions[name] = (typ, skip_reason(name, typ, desc, "0001511737-26-000076"))
    assert decisions["ui-20261106.htm"] == ("8-K", None)              # main document: download
    assert decisions["exhibit991-093026.htm"] == ("EX-99.1", None)     # press release: download
    assert decisions["ubntlogo_jpg.jpg"][1] is not None                 # image: record only
    assert decisions["0001511737-26-000076.txt"][1] is not None         # full-submission duplicate: record only


def test_sec_columns_to_rows():
    rows = columns_to_rows({"accessionNumber": ["a", "b"], "form": ["8-K", "10-Q"]})
    assert rows == [{"accessionNumber": "a", "form": "8-K"}, {"accessionNumber": "b", "form": "10-Q"}]
