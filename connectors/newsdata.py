"""NewsData.io connector: market news whose headline names a watchlist company.

How it searches (free plan):
  - Uses the /market endpoint with qInTitle, e.g.  "Arista Networks" OR Cloudflare OR Palantir.
    Ticker search, the archive, and full article text are paid features.
  - Several companies are combined into one query (up to ~500 characters), so a
    large watchlist needs only a few queries.
  - Each request returns up to 10 articles and costs 1 credit (free plan: about
    200 credits a day). The run stops at --max-credits.
  - Free plan news is delayed about 12 hours.

Each article is assigned to a ticker if NewsData tagged it with that symbol, or
the headline contains the company's search name. Articles matching no watchlist
company are dropped. If a ticker has no news on the day, the latest earlier day's
news is returned instead, marked extra["is_latest_fallback"] = True.

Search names come from watchlist.json ("name"), otherwise from the SEC company list.

Run it:
    python -m connectors.newsdata
    python -m connectors.newsdata --day 2026-11-08 --max-batches 5

Needs NEWSDATA_API in .env. Only headlines, short descriptions and links are stored.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

if __package__ in (None, ""):  # run as a plain script (e.g. VS Code Run button): make the project root importable
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import RAW_DIR, Connector, Item, load_watchlist_entries

log = logging.getLogger("connectors.newsdata")

BASE_URL = "https://newsdata.io/api/1"
SEC_TICKERS_FILE = RAW_DIR / "sec" / "company_tickers.json"  # saved by the SEC connector
SEC_TICKERS_URL = "https://www.sec.gov/files/company_tickers.json"
MAX_QUERY_CHARS = 500  # NewsData limit is 512
NO_RETRY_STATUSES = {400, 401, 403, 404, 422}
LEGAL_SUFFIXES = {"inc", "incorporated", "corp", "corporation", "co", "company", "ltd", "limited",
                  "plc", "holdings", "holding", "group", "sa", "nv", "ag", "se", "lp", "llc", "the"}


def clean_company_name(title: str) -> str:
    """'Arista Networks, Inc.' -> 'Arista Networks'."""
    words = re.sub(r"[,.]", " ", title).split()
    while words and words[-1].lower() in LEGAL_SUFFIXES:
        words.pop()
    return " ".join(words) or title


class NewsDataConnector(Connector):
    name = "newsdata"

    def __init__(
        self,
        entries: list[dict] | None = None,
        lookback_days: int = 3,
        max_batches_per_query: int = 3,
        max_credits: int = 50,
        seconds_between_calls: float = 1.0,
        raw_dir: Path = RAW_DIR / "newsdata",
        fallback_to_latest: bool = True,
    ):
        entries = entries if entries is not None else load_watchlist_entries()
        super().__init__([e["ticker"] for e in entries])
        load_dotenv()
        api_key = (os.environ.get("NEWSDATA_API") or os.environ.get("NEWSDATA_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("NEWSDATA_API is not set. Add 'NEWSDATA_API=your_key' to .env")

        self.session = requests.Session()
        self.session.headers.update({"X-ACCESS-KEY": api_key})  # header, so the key never appears in logs
        self.lookback_days = lookback_days
        self.max_batches_per_query = max_batches_per_query
        self.max_credits = max_credits
        self.min_interval = seconds_between_calls
        self.fallback_to_latest = fallback_to_latest
        self.raw_dir = Path(raw_dir)
        self.credits_used = 0
        self.batches_per_query = max_batches_per_query
        self._last_request = 0.0
        self.search_names = self._resolve_names(entries)

    # ---------- names ----------

    def _resolve_names(self, entries: list[dict]) -> dict[str, str]:
        """ticker -> name used in headline searches."""
        missing = [e["ticker"] for e in entries if not e["name"]]
        sec_titles: dict[str, str] = {}
        if missing:
            sec_titles = self._load_sec_titles()
        names = {}
        for e in entries:
            name = e["name"] or (clean_company_name(sec_titles[e["ticker"]]) if e["ticker"] in sec_titles else None)
            if not name:
                msg = f"{e['ticker']}: no company name found; searching by ticker, which may match unrelated headlines"
                log.warning(msg)
                self.errors.append(msg)
                name = e["ticker"]
            names[e["ticker"]] = name
        return names

    def _load_sec_titles(self) -> dict[str, str]:
        try:
            if SEC_TICKERS_FILE.exists():
                rows = json.loads(SEC_TICKERS_FILE.read_text(encoding="utf-8")).values()
            else:
                ua = os.environ.get("SEC_USER_AGENT", "").strip()
                resp = requests.get(SEC_TICKERS_URL, headers={"User-Agent": ua}, timeout=30)
                resp.raise_for_status()
                rows = resp.json().values()
            return {r["ticker"].upper(): r["title"] for r in rows}
        except Exception as exc:
            log.warning("Could not load SEC company names: %s", exc)
            return {}

    # ---------- HTTP ----------

    def _get(self, path: str, params: dict, retries: int = 4) -> requests.Response:
        url = f"{BASE_URL}{path}"
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()

            backoff = 2 ** attempt
            try:
                resp = self.session.get(url, params=params, timeout=30)
                self.credits_used += 1
                if resp.status_code == 200:
                    return resp
                try:
                    detail = resp.json().get("results", {}).get("message", "")
                except ValueError:
                    detail = ""
                last_error = requests.HTTPError(f"HTTP {resp.status_code} {detail}".strip(), response=resp)
                if resp.status_code in NO_RETRY_STATUSES:
                    raise last_error
                if resp.status_code == 429:
                    backoff = max(backoff, 60)
            except requests.HTTPError:
                raise
            except requests.RequestException as exc:
                last_error = exc

            if attempt < retries:
                log.warning("Request failed (%s). Retrying in %ss", last_error, backoff)
                time.sleep(backoff)
        raise last_error  # type: ignore[misc]

    def _save_raw(self, relative_path: str, content: bytes) -> None:
        path = self.raw_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)

    # ---------- queries ----------

    @staticmethod
    def _term(name: str) -> str:
        return f'"{name}"' if " " in name else name

    def _build_queries(self) -> list[list[str]]:
        """One query per ticker when the credit budget allows it, otherwise OR-groups.

        Separate queries are fairer: in a shared query, a company with lots of news
        (e.g. Apple) fills the 10 results per batch and crowds out the others.
        """
        if len(self.tickers) <= self.max_credits:
            self.batches_per_query = max(1, min(self.max_batches_per_query, self.max_credits // len(self.tickers)))
            return [[t] for t in self.tickers]

        self.batches_per_query = self.max_batches_per_query
        groups: list[list[str]] = []
        current: list[str] = []
        length = 0
        for ticker, name in self.search_names.items():
            add = len(self._term(name)) + (4 if current else 0)  # " OR "
            if current and length + add > MAX_QUERY_CHARS:
                groups.append(current)
                current, length = [], 0
                add = len(self._term(name))
            current.append(ticker)
            length += add
        if current:
            groups.append(current)
        return groups

    def _match(self, article: dict, tickers: list[str]) -> list[str]:
        title = article.get("title") or ""
        tagged = {s.upper() for s in (article.get("symbol") or [])}
        matched = []
        for t in tickers:
            pattern = r"\b" + re.escape(self.search_names[t]) + r"\b"
            if t in tagged or re.search(pattern, title, re.IGNORECASE):
                matched.append(t)
        return matched

    # ---------- fetch ----------

    def fetch(self, day: date) -> list[Item]:
        day_str = day.isoformat()
        window_start = (day - timedelta(days=self.lookback_days)).isoformat()
        groups = self._build_queries()
        log.info("Searching %d ticker(s) in %d query group(s), %s to %s",
                 len(self.tickers), len(groups), window_start, day_str)

        stories: dict[str, dict] = {}  # dedupe key -> {"article", "tickers", "published"}
        for g, tickers in enumerate(groups, start=1):
            query = " OR ".join(self._term(self.search_names[t]) for t in tickers)
            params = {"qInTitle": query, "language": "en"}
            for batch_no in range(1, self.batches_per_query + 1):
                if self.credits_used >= self.max_credits:
                    msg = f"stopped at --max-credits {self.max_credits}; group {g} batch {batch_no} onwards not fetched"
                    log.warning(msg)
                    self.errors.append(msg)
                    return self._select(stories, day_str)
                try:
                    resp = self._get("/market", params)
                except Exception as exc:
                    msg = f"group {g} ({','.join(tickers)}) batch {batch_no}: {exc}"
                    log.error(msg)
                    self.errors.append(msg)
                    break
                self._save_raw(f"{day_str}/market/group{g:03d}_batch{batch_no:02d}.json", resp.content)
                data = resp.json()
                articles = data.get("results") or []

                oldest = None
                for a in articles:
                    published = self._parse_date(a.get("pubDate"))
                    if published is None:
                        continue
                    oldest = published if oldest is None else min(oldest, published)
                    if a.get("duplicate") or published.date().isoformat() > day_str:
                        continue
                    matched = self._match(a, self.tickers)  # tag every watchlist company the headline names
                    if not matched:
                        continue
                    key = self._dedupe_key(a)
                    if key in stories:
                        stories[key]["tickers"].update(matched)
                    else:
                        stories[key] = {"article": a, "tickers": set(matched), "published": published}

                log.info("Group %d batch %d: %d article(s)", g, batch_no, len(articles))
                next_batch = data.get("nextPage")  # NewsData calls each batch a "page"
                if not next_batch or (oldest and oldest.date().isoformat() < window_start):
                    break
                params = {**params, "page": next_batch}
        return self._select(stories, day_str)

    @staticmethod
    def _dedupe_key(article: dict) -> str:
        """Same story from Google News arrives as 'Headline - Publisher'; strip that suffix."""
        title = (article.get("title") or "").strip()
        if article.get("source_id") == "google":
            title = re.sub(r"\s+-\s+[^-]{2,60}$", "", title)
        title = re.sub(r"\W+", " ", title).strip().lower()
        return title or article.get("link", "")

    @staticmethod
    def _parse_date(value: str | None) -> datetime | None:
        if not value:
            return None
        try:
            return datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=timezone.utc)
        except ValueError:
            return None

    def _select(self, stories: dict[str, dict], day_str: str) -> list[Item]:
        """Per ticker: stories from the day, or else from that ticker's latest earlier day."""
        chosen_day: dict[str, str | None] = {}
        for t in self.tickers:
            days = {s["published"].date().isoformat() for s in stories.values() if t in s["tickers"]}
            if day_str in days or not self.fallback_to_latest:
                chosen_day[t] = day_str
            else:
                chosen_day[t] = max(days, default=None)

        items = []
        for s in stories.values():
            pub_day = s["published"].date().isoformat()
            keep = [t for t in self.tickers if t in s["tickers"] and chosen_day[t] == pub_day]
            if not keep:
                continue
            a = s["article"]
            items.append(Item(
                source=self.name,
                source_id=a.get("article_id") or a.get("link", ""),
                tickers=keep,
                title=a.get("title", ""),
                url=a.get("link", ""),
                published_at=s["published"].isoformat(),
                item_type="news",
                extra={
                    "publisher": a.get("source_name") or a.get("source_id"),
                    "description": a.get("description"),
                    "sentiment": a.get("sentiment"),
                    "symbols_tagged": a.get("symbol"),
                    "category": a.get("category"),
                    "is_latest_fallback": pub_day != day_str,
                },
            ))
        return sorted(items, key=lambda it: (self.tickers.index(it.tickers[0]), it.published_at), reverse=False)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch NewsData.io market news for the watchlist")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD or 'today' (default)")
    parser.add_argument("--lookback", type=int, default=3, help="Days to look back when a ticker has no news on --day (default 3)")
    parser.add_argument("--max-batches", type=int, default=3, help="Batches (10 articles each) per query group (default 3)")
    parser.add_argument("--max-credits", type=int, default=50, help="Stop after this many API credits (default 50)")
    parser.add_argument("--no-fallback", action="store_true", help="Only return news from --day")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)

    connector = NewsDataConnector(
        lookback_days=args.lookback,
        max_batches_per_query=args.max_batches,
        max_credits=args.max_credits,
        fallback_to_latest=not args.no_fallback,
    )
    items = connector.fetch(day)

    out = connector.raw_dir / day.isoformat() / "items.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([it.to_dict() for it in items], indent=2), encoding="utf-8")

    print(f"\n{len(items)} story(ies) for {day} (* = none that day, showing the latest earlier day)")
    for it in items:
        mark = "*" if it.extra.get("is_latest_fallback") else " "
        print(f" {mark}{','.join(it.tickers):<10} {it.published_at[:16]}  {(it.extra.get('publisher') or '')[:14]:<14} {it.title[:80]}")
    found = {t for it in items for t in it.tickers}
    missing = [t for t in connector.tickers if t not in found]
    if missing:
        print(f"\nNo news found for: {', '.join(missing)}")
    print(f"\nCredits used: {connector.credits_used}")
    if connector.errors:
        print(f"\n{len(connector.errors)} error(s):")
        for err in connector.errors:
            print("  " + err)
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
