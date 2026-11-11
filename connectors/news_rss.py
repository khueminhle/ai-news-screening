"""RSS news connector: headlines from big outlets only (Reuters, Bloomberg, WSJ, FT, CNBC, ...).

How it searches (free, no API key):
  - Company news: one Google News RSS search per watchlist ticker, restricted to the sites in
    config/news_sources.json, e.g.  "Fabrinet" (site:reuters.com OR site:bloomberg.com ...) when:2d
    The search words are the ticker's "news_query" in watchlist.json, else its name.
  - Market news: the market_queries in the config (same site restriction) plus the outlets' own
    RSS feeds (WSJ Markets, CNBC Top News).
  - Every result is checked against the site list again (by the outlet's web address), so only
    those outlets get through. Quote pages and podcasts are skipped by title.

Only the headline, outlet, link and time are stored: no summary, no article text (the articles are
behind paywalls). Claude classifies relevance and importance from the headline (run_classify).

Run it:
    python -m connectors.news_rss
    python -m connectors.news_rss --day 2026-11-10
"""
from __future__ import annotations

import argparse
import json
import logging
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, time as dtime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path
from urllib.parse import urlparse

import requests

if __package__ in (None, ""):
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import PROJECT_ROOT, RAW_DIR, Connector, Item, load_watchlist_entries

log = logging.getLogger("connectors.news_rss")

CONFIG_PATH = PROJECT_ROOT / "config" / "news_sources.json"
GOOGLE_NEWS = "https://news.google.com/rss/search"
HEADERS = {"User-Agent": "Mozilla/5.0 (research digest; RSS reader)"}


def load_config(path: Path = CONFIG_PATH) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


def site_of(url: str | None, sites: dict[str, str]) -> str | None:
    """'https://www.reuters.com/...' -> 'Reuters' if the host is (a subdomain of) a listed site, else None."""
    host = (urlparse(url or "").hostname or "").lower()
    for domain, label in sites.items():
        if host == domain or host.endswith("." + domain):
            return label
    return None


def strip_outlet(title: str) -> str:
    """'Nvidia shares rebound 5% - CNBC' -> 'Nvidia shares rebound 5%' (Google News adds the outlet)."""
    return re.sub(r"\s+-\s+[^-]{2,60}$", "", title.strip()).strip()


def parse_feed(content: bytes, sites: dict[str, str], feed_outlet: str | None = None) -> list[dict]:
    """RSS items -> [{title, url, guid, publisher, published}] from listed outlets only.

    Google News items carry <source url="https://www.reuters.com">Reuters</source>; an outlet's own feed
    (feed_outlet given) is trusted as that outlet.
    """
    out = []
    for node in ET.fromstring(content).iter("item"):
        raw_title = (node.findtext("title") or "").strip()
        link = (node.findtext("link") or "").strip()
        pub = node.findtext("pubDate")
        if not (raw_title and link and pub):
            continue
        if feed_outlet:
            publisher = feed_outlet
        else:
            src = node.find("source")
            publisher = site_of(src.get("url") if src is not None else None, sites)
            if not publisher:
                continue
        title = strip_outlet(raw_title) if not feed_outlet else raw_title
        if not title:
            continue
        try:
            published = parsedate_to_datetime(pub.strip()).astimezone(timezone.utc)
        except (TypeError, ValueError):
            continue
        out.append({"title": title, "url": link, "guid": (node.findtext("guid") or link).strip(),
                    "publisher": publisher, "published": published})
    return out


class NewsRssConnector(Connector):
    name = "news_rss"

    def __init__(self, entries: list[dict] | None = None, config: dict | None = None,
                 raw_dir: Path = RAW_DIR / "news_rss", seconds_between_calls: float = 1.0):
        entries = entries if entries is not None else load_watchlist_entries()
        super().__init__([e["ticker"] for e in entries])
        self.cfg = config or load_config()
        self.sites: dict[str, str] = self.cfg["sites"]
        self.queries = {e["ticker"]: e.get("news_query") or f'"{e.get("name") or e["ticker"]}"' for e in entries}
        self.raw_dir = Path(raw_dir)
        self.min_interval = seconds_between_calls
        self.session = requests.Session()
        self.session.headers.update(HEADERS)
        self._last = 0.0
        skip = self.cfg.get("skip_title_patterns", [])
        self.skip = re.compile("|".join(re.escape(p) for p in skip), re.I) if skip else None

    # ---------- HTTP ----------

    def _get(self, url: str, params: dict | None = None, retries: int = 3) -> bytes:
        last: Exception | None = None
        for attempt in range(retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last)
            if wait > 0:
                time.sleep(wait)
            self._last = time.monotonic()
            try:
                resp = self.session.get(url, params=params, timeout=30)
                if resp.status_code == 200:
                    return resp.content
                last = requests.HTTPError(f"HTTP {resp.status_code}")
                if resp.status_code in (400, 401, 403, 404):
                    break
            except requests.RequestException as exc:
                last = exc
            if attempt < retries:
                time.sleep(2 ** attempt)
        raise last  # type: ignore[misc]

    def _save_raw(self, day_str: str, name: str, content: bytes) -> None:
        path = self.raw_dir / day_str / f"{re.sub(r'[^A-Za-z0-9_-]+', '_', name)}.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    def search_query(self, words: str) -> str:
        sites = " OR ".join(f"site:{d}" for d in self.sites)
        return f"({words}) ({sites}) when:{self.cfg.get('lookback_days', 2)}d"

    # ---------- fetch ----------

    def _search(self, words: str) -> bytes:
        return self._get(GOOGLE_NEWS, {"q": self.search_query(words), "hl": "en-US", "gl": "US", "ceid": "US:en"})

    def fetch(self, day: date) -> list[Item]:
        day_str = day.isoformat()
        end = datetime.combine(day, dtime.max, timezone.utc)
        start = end - timedelta(days=self.cfg.get("lookback_days", 2))
        found: dict[str, dict] = {}        # dedupe key -> story (tickers merged)

        def keep(stories: list[dict], ticker: str | None, limit: int, origin: str) -> None:
            fresh = [s for s in stories if start <= s["published"] <= end and not (self.skip and self.skip.search(s["title"]))]
            for s in sorted(fresh, key=lambda s: s["published"], reverse=True)[:limit]:
                key = f"{s['publisher']}|{s['title'].lower()}"
                story = found.setdefault(key, {**s, "tickers": set(), "origin": origin})
                if ticker:
                    story["tickers"].add(ticker)

        for ticker, words in self.queries.items():
            try:
                content = self._search(words)
                self._save_raw(day_str, f"ticker_{ticker}", content)
                keep(parse_feed(content, self.sites), ticker, self.cfg.get("max_per_ticker", 20), f"search:{ticker}")
            except Exception as exc:
                self.errors.append(f"{ticker}: {type(exc).__name__}: {exc}")
                log.warning("news search %s failed: %s", ticker, exc)

        for n, words in enumerate(self.cfg.get("market_queries", []), 1):
            try:
                content = self._search(words)
                self._save_raw(day_str, f"market_{n}", content)
                keep(parse_feed(content, self.sites), None, self.cfg.get("max_per_market_query", 30), f"market:{n}")
            except Exception as exc:
                self.errors.append(f"market query {n}: {type(exc).__name__}: {exc}")
                log.warning("market search %s failed: %s", n, exc)

        for outlet, url in self.cfg.get("feeds", {}).items():
            try:
                content = self._get(url)
                self._save_raw(day_str, f"feed_{outlet}", content)
                label = outlet.split()[0]          # "WSJ Markets" -> "WSJ"
                keep(parse_feed(content, self.sites, feed_outlet=label), None,
                     self.cfg.get("max_per_market_query", 30), f"feed:{outlet}")
            except Exception as exc:
                self.errors.append(f"feed {outlet}: {type(exc).__name__}: {exc}")
                log.warning("feed %s failed: %s", outlet, exc)

        items = [
            Item(source=self.name, source_id=s["guid"], tickers=sorted(s["tickers"]), title=s["title"], url=s["url"],
                 published_at=s["published"].isoformat(), item_type="news" if s["tickers"] else "market_news",
                 extra={"publisher": s["publisher"], "found_by": s["origin"]})
            for s in found.values()
        ]
        log.info("news_rss: %d headlines (%d company, %d market)", len(items),
                 sum(1 for i in items if i.tickers), sum(1 for i in items if not i.tickers))
        return sorted(items, key=lambda it: it.published_at, reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch headlines from big outlets via RSS")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD or 'today'")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)
    c = NewsRssConnector()
    items = c.fetch(day)
    for it in items:
        print(f"{it.published_at[:16]}  {','.join(it.tickers) or 'MARKET':<8} {it.extra['publisher']:<12} {it.title[:90]}")
    print(f"\n{len(items)} headlines, {len(c.errors)} error(s)")
    for e in c.errors:
        print("  - " + e)


if __name__ == "__main__":
    main()
