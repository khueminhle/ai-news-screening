"""Federal Reserve connector: official central bank releases, with FULL TEXT.

Reads the Fed's public RSS feeds, then downloads each release's page and
extracts the complete text. Fed publications are US government works, so
storing the full text is fine.

Feeds:
  monetary   FOMC statements, FOMC minutes, discount rate decisions
  speeches   speeches by Fed Chair, Governors (their views on the economy and rates)
  testimony  Fed officials' testimony to Congress

Returns every release from the last --lookback days (default 30). If a feed has
nothing in that window, its latest release is returned instead, marked with
extra["is_latest_fallback"] = True.

The latest FOMC statement (the current rate decision) is ALWAYS returned, even
when it is older than the window, marked extra["always_included_latest_fomc_statement"] = True.

Run it:
    python -m connectors.fed
    python -m connectors.fed --lookback 90

Files:
    data/raw/fed/documents/<id>.txt    full text (one file per release, kept across runs)
    data/raw/fed/documents/<id>.html   original page
    data/raw/fed/<day>/items.json      this run's list
    data/raw/fed/<day>/feeds/*.xml     raw RSS feeds
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
import xml.etree.ElementTree as ET
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from pathlib import Path

import requests
from dotenv import load_dotenv

if __package__ in (None, ""):  # run as a plain script (e.g. VS Code Run button): make the project root importable
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import RAW_DIR, Connector, Item, to_utc_z, write_items_json
from rag.parse import parse_document

log = logging.getLogger("connectors.fed")

FEEDS = {
    "monetary": "https://www.federalreserve.gov/feeds/press_monetary.xml",
    "speeches": "https://www.federalreserve.gov/feeds/speeches.xml",
    "testimony": "https://www.federalreserve.gov/feeds/testimony.xml",
}


def classify(feed: str, title: str) -> str:
    t = title.lower()
    if feed == "monetary":
        if "fomc statement" in t:
            return "fomc_statement"
        if "minutes of the federal open market committee" in t:
            return "fomc_minutes"
        return "fed_monetary_release"
    return {"speeches": "fed_speech", "testimony": "fed_testimony"}[feed]


class FedConnector(Connector):
    name = "federal_reserve"

    def __init__(self, lookback_days: int = 30, raw_dir: Path = RAW_DIR / "fed",
                 seconds_between_calls: float = 1.0, fallback_to_latest: bool = True):
        super().__init__(tickers=[])  # macro source: not tied to the watchlist
        load_dotenv()
        contact = os.environ.get("SEC_USER_AGENT", "").strip()
        self.session = requests.Session()
        self.session.headers["User-Agent"] = f"investment-signal-agent personal research ({contact})" if contact \
            else "investment-signal-agent personal research"
        self.lookback_days = lookback_days
        self.raw_dir = Path(raw_dir)
        self.min_interval = seconds_between_calls
        self.fallback_to_latest = fallback_to_latest
        self._last_request = 0.0

    def _get(self, url: str, retries: int = 3) -> requests.Response:
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self.session.get(url, timeout=30)
                if resp.status_code == 200:
                    return resp
                last_error = requests.HTTPError(f"HTTP {resp.status_code} for {url}", response=resp)
                if resp.status_code == 404:
                    raise last_error
            except requests.HTTPError:
                raise
            except requests.RequestException as exc:
                last_error = exc
            if attempt < retries:
                log.warning("Request failed (%s). Retrying in %ss", last_error, 2 ** attempt)
                time.sleep(2 ** attempt)
        raise last_error  # type: ignore[misc]

    # ---------- feeds ----------

    def _read_feed(self, feed: str, url: str, day_str: str) -> list[dict]:
        resp = self._get(url)
        path = self.raw_dir / day_str / "feeds" / f"{feed}.xml"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(resp.content)

        entries = []
        for node in ET.fromstring(resp.content).iter("item"):
            title = (node.findtext("title") or "").strip()
            link = (node.findtext("link") or "").strip()
            pub = node.findtext("pubDate")
            if not (title and link and pub):
                continue
            published = parsedate_to_datetime(pub.strip()).astimezone(timezone.utc)
            entries.append({"feed": feed, "title": title, "link": link, "published": published,
                            "description": (node.findtext("description") or "").strip()})
        return entries

    # ---------- full text ----------

    def _full_text(self, link: str, doc_type: str) -> dict:
        """Download the release page once, extract its text (tables kept as rows), save .html/.txt/.blocks.json."""
        doc_id = re.sub(r"\.htm.*$", "", link.rstrip("/").rsplit("/", 1)[-1])  # e.g. monetary20261028a
        folder = self.raw_dir / "documents"
        html_path, txt_path = folder / f"{doc_id}.html", folder / f"{doc_id}.txt"
        blocks_path = folder / f"{doc_id}.blocks.json"

        if not html_path.exists():  # downloaded once, kept for re-processing
            folder.mkdir(parents=True, exist_ok=True)
            html_path.write_bytes(self._get(link).content)  # bytes: the parser detects the encoding
        if not blocks_path.exists():
            parsed = parse_document(html_path, doc_type, root_selector="#article", drop_lines=("Share", "Watch Live"))
            blocks_path.write_text(json.dumps(parsed, ensure_ascii=False, indent=1), encoding="utf-8")
            txt_path.write_text("\n\n".join(b["content"] for b in parsed["blocks"]), encoding="utf-8")
        text = txt_path.read_text(encoding="utf-8")
        return {"doc_id": doc_id, "text_chars": len(text), "text_path": str(txt_path),
                "html_path": str(html_path), "blocks_path": str(blocks_path)}

    # ---------- fetch ----------

    def fetch(self, day: date) -> list[Item]:
        day_str = day.isoformat()
        day_end = datetime.combine(day, datetime.max.time(), tzinfo=timezone.utc)
        window_start = day_end - timedelta(days=self.lookback_days)

        items: list[Item] = []
        for feed, url in FEEDS.items():
            try:
                entries = [e for e in self._read_feed(feed, url, day_str) if e["published"] <= day_end]
            except Exception as exc:
                msg = f"feed {feed}: {exc}"
                log.error(msg)
                self.errors.append(msg)
                continue

            chosen = [e for e in entries if e["published"] >= window_start]
            is_fallback = False
            if not chosen and self.fallback_to_latest and entries:
                chosen, is_fallback = [max(entries, key=lambda e: e["published"])], True

            # The current rate decision always matters, however old: always include the latest FOMC statement.
            if feed == "monetary":
                statements = [e for e in entries if classify(feed, e["title"]) == "fomc_statement"]
                latest_statement = max(statements, key=lambda e: e["published"], default=None)
                if latest_statement and latest_statement not in chosen:
                    latest_statement["always_included"] = True
                    chosen.append(latest_statement)
                elif not latest_statement:
                    msg = "monetary feed: no FOMC statement found in the feed"
                    log.warning(msg)
                    self.errors.append(msg)
            log.info("%s: %d release(s)%s", feed, len(chosen), " (latest, outside window)" if is_fallback else "")

            for e in chosen:
                doc_type = classify(feed, e["title"])
                try:
                    doc = self._full_text(e["link"], doc_type)
                except Exception as exc:
                    msg = f"{feed} {e['link']}: could not download text: {exc}"
                    log.error(msg)
                    self.errors.append(msg)
                    doc = {"text_chars": 0}
                items.append(Item(
                    source=self.name,
                    source_id=e["link"],
                    tickers=[],
                    title=e["title"],
                    url=e["link"],
                    published_at=to_utc_z(e["published"]),
                    item_type=doc_type,
                    extra={
                        "feed": feed,
                        "rss_description": e["description"],
                        **doc,
                        "is_latest_fallback": is_fallback or e.get("always_included", False),
                        "always_included_latest_fomc_statement": e.get("always_included", False),
                    },
                ))
        return sorted(items, key=lambda it: it.published_at, reverse=True)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Federal Reserve releases with full text")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD or 'today' (default)")
    parser.add_argument("--lookback", type=int, default=30, help="Days of releases to return (default 30)")
    parser.add_argument("--no-fallback", action="store_true", help="Do not return a feed's latest release when the window is empty")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)

    connector = FedConnector(lookback_days=args.lookback, fallback_to_latest=not args.no_fallback)
    items = connector.fetch(day)

    out = connector.raw_dir / day.isoformat() / "items.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([it.to_dict() for it in items], indent=2, ensure_ascii=False), encoding="utf-8")

    print(f"\n{len(items)} Fed release(s) in the {args.lookback} days to {day} (* = outside the window, latest of its feed)")
    for it in items:
        mark = "*" if it.extra.get("is_latest_fallback") else " "
        print(f" {mark}{it.published_at[:10]}  {it.item_type:<20} {it.extra['text_chars']:>6} chars  {it.title[:70]}")
    if connector.errors:
        print(f"\n{len(connector.errors)} error(s):")
        for err in connector.errors:
            print("  " + err)
    print(f"\nFull texts: {connector.raw_dir / 'documents'}")
    print(f"Saved: {out}")


if __name__ == "__main__":
    main()
