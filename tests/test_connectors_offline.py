"""Connector logic on saved real responses (no network, no API keys)."""
import json
from datetime import date
from pathlib import Path

from bs4 import BeautifulSoup

from connectors.news_rss import NewsRssConnector, parse_feed, site_of, strip_outlet
from connectors.sec import columns_to_rows, skip_reason

FIXTURES = Path(__file__).parent / "fixtures"


SITES = {"reuters.com": "Reuters", "bloomberg.com": "Bloomberg", "wsj.com": "WSJ", "cnbc.com": "CNBC"}


def test_rss_keeps_only_listed_outlets():
    assert site_of("https://www.reuters.com", SITES) == "Reuters"
    assert site_of("https://notreuters.com", SITES) is None
    assert strip_outlet("Baird downgrades Nvidia - Bloomberg.com") == "Baird downgrades Nvidia"
    stories = parse_feed((FIXTURES / "google_news_rss.xml").read_bytes(), SITES)
    assert {s["publisher"] for s in stories} == {"CNBC", "Bloomberg", "WSJ", "Reuters"}    # the blog is dropped


def test_rss_connector_window_skip_patterns_and_dedupe(monkeypatch):
    cfg = {"sites": SITES, "lookback_days": 2, "max_per_ticker": 20, "max_per_market_query": 30,
           "market_queries": ["stocks"], "feeds": {}, "skip_title_patterns": ["stock price"]}
    c = NewsRssConnector(entries=[{"ticker": "NVDA", "name": "Nvidia"}, {"ticker": "AAPL", "name": "Apple"}],
                         config=cfg, raw_dir=Path("unused"), seconds_between_calls=0)
    xml = (FIXTURES / "google_news_rss.xml").read_bytes()
    monkeypatch.setattr(c, "_search", lambda words: xml)
    monkeypatch.setattr(c, "_save_raw", lambda *a: None)
    assert c.queries == {"NVDA": '"Nvidia"', "AAPL": '"Apple"'}
    items = c.fetch(date(2026, 11, 11))
    titles = {it.title for it in items}
    assert titles == {"Nvidia shares rebound 5% as Huang faces scrutiny", "Baird downgrades Nvidia"}  # old + quote page gone
    assert all(it.tickers == ["AAPL", "NVDA"] and it.item_type == "news" for it in items)          # one item, tickers merged
    assert all(it.extra["publisher"] in ("CNBC", "Bloomberg") for it in items) and not c.errors


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
