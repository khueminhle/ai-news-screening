"""Finnhub connector: company news, market/macro news, analyst ratings, earnings calendar.

Reads every ticker in watchlist/watchlist.json and returns:
  - company news for the given day. If a ticker has no news that day, it returns
    the news from the most recent earlier day within --lookback days, marked with
    extra["is_latest_fallback"] = True.
  - general market and macro news (economy, Fed, bonds), not tied to a ticker.
  - analyst rating counts per ticker (strong buy ... strong sell), monthly, with the
    change from the previous month. Upgrades/downgrades and price targets are paid only.
  - upcoming earnings dates for watchlist tickers, from the day to --earnings-ahead days later.

Run it:
    python -m connectors.finnhub                     # today
    python -m connectors.finnhub --day 2026-11-06
    python -m connectors.finnhub --lookback 14 --earnings-ahead 60

Needs FINNHUB_API in .env. Free tier: about 60 calls a minute, so the connector
spaces calls about 1.1 s apart (roughly 1 minute per 55 tickers).
Only headlines, summaries and links are stored, never full articles.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests
from dotenv import load_dotenv

if __package__ in (None, ""):  # run as a plain script (e.g. VS Code Run button): make the project root importable
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import RAW_DIR, Connector, Item

log = logging.getLogger("connectors.finnhub")

BASE_URL = "https://finnhub.io/api/v1"
NO_RETRY_STATUSES = {400, 401, 403, 404}  # bad request, bad key, no access, not found
EARNINGS_ROW_CAP = 1500  # Finnhub returns at most this many calendar rows per call


class FinnhubConnector(Connector):
    name = "finnhub"

    def __init__(
        self,
        tickers: list[str] | None = None,
        lookback_days: int = 7,
        earnings_ahead_days: int = 120,
        calls_per_minute: int = 55,
        raw_dir: Path = RAW_DIR / "finnhub",
        fallback_to_latest: bool = True,
    ):
        super().__init__(tickers)
        load_dotenv()
        api_key = (os.environ.get("FINNHUB_API") or os.environ.get("FINNHUB_API_KEY") or "").strip()
        if not api_key:
            raise RuntimeError("FINNHUB_API is not set. Add 'FINNHUB_API=your_key' to .env")

        self.session = requests.Session()
        # Key goes in a header, not the URL, so it never shows up in logs or error messages.
        self.session.headers.update({"X-Finnhub-Token": api_key})
        self.lookback_days = lookback_days
        self.earnings_ahead_days = earnings_ahead_days
        self.fallback_to_latest = fallback_to_latest
        self.raw_dir = Path(raw_dir)
        self.min_interval = 60.0 / calls_per_minute
        self._last_request = 0.0

    # ---------- HTTP ----------

    def _get(self, path: str, params: dict, retries: int = 4) -> requests.Response:
        """GET with rate limiting and back-off. Honours Retry-After on HTTP 429."""
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
                if resp.status_code == 200:
                    return resp
                last_error = requests.HTTPError(f"HTTP {resp.status_code} for {path} {params}", response=resp)
                if resp.status_code in NO_RETRY_STATUSES:
                    raise last_error
                if resp.status_code == 429:
                    backoff = max(backoff, int(resp.headers.get("Retry-After", 0) or 0), 10)
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

    # ---------- fetch ----------

    def fetch(self, day: date) -> list[Item]:
        news = self.fetch_news(day)
        market = self.fetch_market_news(day)
        ratings = self.fetch_recommendations(day)
        earnings = self.fetch_earnings(day)
        return news + market + ratings + earnings

    def fetch_market_news(self, day: date) -> list[Item]:
        """General market and macro news (economy, Fed, bonds, markets). Not tied to a ticker.

        Finnhub returns about the latest 100 stories. Keeps the day's stories, or the
        latest earlier day's if there are none.
        """
        day_str = day.isoformat()
        try:
            resp = self._get("/news", {"category": "general"})
        except Exception as exc:
            msg = f"market news: {exc}"
            log.error(msg)
            self.errors.append(msg)
            return []
        self._save_raw(f"{day_str}/market_news.json", resp.content)

        dated = []
        for s in resp.json():
            if s.get("datetime") and s.get("headline"):
                published = datetime.fromtimestamp(s["datetime"], tz=timezone.utc)
                if published.date().isoformat() <= day_str:
                    dated.append((published, s))
        chosen_day = day_str
        if self.fallback_to_latest and not any(p.date().isoformat() == day_str for p, _ in dated):
            chosen_day = max((p.date().isoformat() for p, _ in dated), default=day_str)

        items = [
            Item(
                source=self.name,
                source_id=str(s.get("id")),
                tickers=[],
                title=s["headline"],
                url=s.get("url", ""),
                published_at=published.isoformat(),
                item_type="market_news",
                extra={
                    "publisher": s.get("source"),
                    "summary": s.get("summary"),
                    "is_latest_fallback": chosen_day != day_str,
                },
            )
            for published, s in dated if published.date().isoformat() == chosen_day
        ]
        log.info("Market news: %d story(ies) for %s", len(items), chosen_day)
        return sorted(items, key=lambda it: it.published_at, reverse=True)

    def fetch_recommendations(self, day: date) -> list[Item]:
        """Analyst rating counts per ticker (monthly), with the change from the month before.

        This is third-party data reported as a fact. Individual upgrades/downgrades and
        price targets are paid Finnhub features and are not fetched.
        """
        day_str = day.isoformat()
        items = []
        for ticker in self.tickers:
            try:
                resp = self._get("/stock/recommendation", {"symbol": ticker})
            except Exception as exc:
                msg = f"recommendations {ticker}: {exc}"
                log.error(msg)
                self.errors.append(msg)
                continue
            self._save_raw(f"{day_str}/recommendations/{ticker}.json", resp.content)
            rows = sorted((r for r in resp.json() if r.get("period", "") <= day_str),
                          key=lambda r: r["period"], reverse=True)
            if not rows:
                continue
            latest, previous = rows[0], (rows[1] if len(rows) > 1 else None)
            fields = ("strongBuy", "buy", "hold", "sell", "strongSell")
            counts = {f: latest.get(f, 0) for f in fields}
            change = {f: counts[f] - previous.get(f, 0) for f in fields} if previous else None
            items.append(Item(
                source=self.name,
                source_id=f"recommendation:{ticker}:{latest['period']}",
                tickers=[ticker],
                title=(f"{ticker} analyst rating counts ({latest['period'][:7]}): "
                       + ", ".join(f"{counts[f]} {f}" for f in fields)),
                url="",
                published_at=latest["period"],
                item_type="analyst_ratings",
                extra={
                    "raw_path": str(self.raw_dir / day_str / "recommendations" / f"{ticker}.json"),
                    "period": latest["period"],
                    "counts": counts,
                    "total_analysts": sum(counts.values()),
                    "previous_period": previous["period"] if previous else None,
                    "change_from_previous": change,
                },
            ))
        log.info("Analyst ratings: %d ticker(s)", len(items))
        return items

    def fetch_news(self, day: date) -> list[Item]:
        day_str = day.isoformat()
        start = (day - timedelta(days=self.lookback_days)).isoformat() if self.fallback_to_latest else day_str
        log.info("Fetching news for %d tickers (%s to %s)", len(self.tickers), start, day_str)

        by_story: dict[str, Item] = {}  # one Item per story, even if several tickers mention it
        for n, ticker in enumerate(self.tickers, start=1):
            try:
                resp = self._get("/company-news", {"symbol": ticker, "from": start, "to": day_str})
                self._save_raw(f"{day_str}/news/{ticker}.json", resp.content)
                found = self._pick_news(ticker, resp.json(), day_str)
                for item in found:
                    key = item.url or item.source_id
                    if key in by_story:
                        if ticker not in by_story[key].tickers:
                            by_story[key].tickers.append(ticker)
                    else:
                        by_story[key] = item
                log.info("[%d/%d] %s: %d story(ies)", n, len(self.tickers), ticker, len(found))
            except Exception as exc:  # one failed ticker never stops the rest
                msg = f"news {ticker}: {exc}"
                log.error(msg)
                self.errors.append(msg)
        return list(by_story.values())

    def _pick_news(self, ticker: str, stories: list[dict], day_str: str) -> list[Item]:
        """Keep stories from day_str. If none, keep stories from the latest earlier day."""
        dated = []
        for s in stories:
            ts = s.get("datetime")
            if not ts or not s.get("headline"):
                continue
            published = datetime.fromtimestamp(ts, tz=timezone.utc)
            dated.append((published.date().isoformat(), published, s))

        on_day = [d for d in dated if d[0] == day_str]
        if on_day or not self.fallback_to_latest:
            chosen, is_fallback = on_day, False
        else:
            latest = max((d[0] for d in dated if d[0] < day_str), default=None)
            chosen, is_fallback = [d for d in dated if d[0] == latest], True

        return [
            Item(
                source=self.name,
                source_id=str(s.get("id")),
                tickers=[ticker],
                title=s["headline"],
                url=s.get("url", ""),
                published_at=published.isoformat(),
                item_type="news",
                extra={
                    "publisher": s.get("source"),
                    "summary": s.get("summary"),
                    "category": s.get("category"),
                    "related": s.get("related"),
                    "is_latest_fallback": is_fallback,
                },
            )
            for _, published, s in chosen
        ]

    def _earnings_rows(self, start: date, end: date) -> list[dict]:
        """Calendar rows for all companies in [start, end].

        Finnhub caps one response at EARNINGS_ROW_CAP rows, so a full response is
        split in half and re-requested until each piece is under the cap.
        """
        resp = self._get("/calendar/earnings", {"from": start.isoformat(), "to": end.isoformat()})
        rows = resp.json().get("earningsCalendar") or []
        if len(rows) >= EARNINGS_ROW_CAP and start < end:
            mid = start + (end - start) // 2
            return self._earnings_rows(start, mid) + self._earnings_rows(mid + timedelta(days=1), end)
        if len(rows) >= EARNINGS_ROW_CAP:
            self.errors.append(f"earnings calendar {start}: hit the {EARNINGS_ROW_CAP}-row cap for a single day; some rows may be missing")
        return rows

    def fetch_earnings(self, day: date) -> list[Item]:
        """Upcoming earnings for watchlist tickers. A few calls cover every ticker."""
        end = day + timedelta(days=self.earnings_ahead_days)
        log.info("Fetching earnings calendar %s to %s", day, end)
        try:
            rows = self._earnings_rows(day, end)
        except Exception as exc:
            msg = f"earnings calendar: {exc}"
            log.error(msg)
            self.errors.append(msg)
            return []
        self._save_raw(f"{day.isoformat()}/earnings_calendar.json",
                       json.dumps({"earningsCalendar": rows}).encode("utf-8"))

        watch = set(self.tickers)
        items = []
        for row in rows:
            symbol = (row.get("symbol") or "").upper()
            if symbol not in watch:
                continue
            items.append(Item(
                source=self.name,
                source_id=f"earnings:{symbol}:{row.get('date')}",
                tickers=[symbol],
                title=f"{symbol} earnings expected {row.get('date')} (Q{row.get('quarter')} {row.get('year')})",
                url="",
                published_at=row.get("date") or "",
                item_type="earnings_calendar",
                extra={
                    "hour": row.get("hour"),  # bmo = before open, amc = after close
                    "eps_estimate": row.get("epsEstimate"),
                    "eps_actual": row.get("epsActual"),
                    "revenue_estimate": row.get("revenueEstimate"),
                    "revenue_actual": row.get("revenueActual"),
                    "quarter": row.get("quarter"),
                    "year": row.get("year"),
                },
            ))
        return sorted(items, key=lambda it: it.published_at)


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch Finnhub company news, market news, analyst ratings and earnings calendar")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD or 'today' (default)")
    parser.add_argument("--lookback", type=int, default=7, help="Days to look back for news when a ticker has none on --day (default 7)")
    parser.add_argument("--earnings-ahead", type=int, default=120, help="Days ahead to check for earnings dates (default 120)")
    parser.add_argument("--no-fallback", action="store_true", help="Only return news from --day")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)

    connector = FinnhubConnector(
        lookback_days=args.lookback,
        earnings_ahead_days=args.earnings_ahead,
        fallback_to_latest=not args.no_fallback,
    )
    items = connector.fetch(day)

    out = connector.raw_dir / day.isoformat() / "items.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([it.to_dict() for it in items], indent=2), encoding="utf-8")

    news = [it for it in items if it.item_type == "news"]
    market = [it for it in items if it.item_type == "market_news"]
    ratings = [it for it in items if it.item_type == "analyst_ratings"]
    earnings = [it for it in items if it.item_type == "earnings_calendar"]

    print(f"\n{len(news)} company news story(ies) for {day} (* = none that day, showing the latest earlier day)")
    for it in news:
        mark = "*" if it.extra.get("is_latest_fallback") else " "
        print(f" {mark}{','.join(it.tickers):<10} {it.published_at[:16]}  {it.extra.get('publisher') or '':<14} {it.title[:80]}")

    print(f"\n{len(market)} market/macro news story(ies) (* = latest earlier day)")
    for it in market[:15]:
        mark = "*" if it.extra.get("is_latest_fallback") else " "
        print(f" {mark}{it.published_at[:16]}  {it.extra.get('publisher') or '':<10} {it.title[:85]}")
    if len(market) > 15:
        print(f"  ... and {len(market) - 15} more in items.json")

    print(f"\nAnalyst rating counts ({len(ratings)} ticker(s))")
    for it in ratings:
        c, ch = it.extra["counts"], it.extra["change_from_previous"] or {}
        diff = ", ".join(f"{k} {v:+d}" for k, v in ch.items() if v) or "no change"
        print(f"  {it.tickers[0]:<6} {it.extra['period'][:7]}  strongBuy {c['strongBuy']:>2}  buy {c['buy']:>2}  "
              f"hold {c['hold']:>2}  sell {c['sell']:>2}  strongSell {c['strongSell']:>2}   vs previous month: {diff}")

    print(f"\n{len(earnings)} upcoming earnings date(s) in the next {args.earnings_ahead} days")
    for it in earnings:
        print(f"  {it.tickers[0]:<6} {it.published_at}  {it.extra.get('hour') or '':<4} EPS est. {it.extra.get('eps_estimate')}")

    if connector.errors:
        print(f"\n{len(connector.errors)} error(s):")
        for err in connector.errors:
            print("  " + err)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
