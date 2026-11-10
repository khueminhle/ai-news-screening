"""yfinance connector: daily prices and volume for the watchlist (plus two market benchmarks).

For each ticker, takes the latest daily bar on or before --day and works out
(in Python, never by the model):
  - 1-day, 5-day, 1-month (21 trading days) and year-to-date % change
  - 52-week high and low, and how far the close is from each
  - volume versus the 20-day average volume

If the latest bar is today's and the US market has not closed yet (4 pm New York
time), it is marked extra["is_partial_session"] = True: those numbers will change.

Benchmarks (S&P 500, Nasdaq-100) are added for market context; --no-benchmarks skips them.

Run it:
    python -m connectors.prices_yfinance
    python -m connectors.prices_yfinance --day 2026-11-06

No key needed. yfinance is an unofficial Yahoo Finance wrapper: it can break without
notice, and is for personal, non-commercial use.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
from datetime import date, datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

if __package__ in (None, ""):  # run as a plain script (e.g. VS Code Run button): make the project root importable
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import RAW_DIR, Connector, Item

log = logging.getLogger("connectors.yfinance")

BENCHMARKS = {"^GSPC": "S&P 500", "^NDX": "Nasdaq-100"}
NEW_YORK = ZoneInfo("America/New_York")
HISTORY_DAYS = 400   # covers the 52-week range and year-to-date
CHUNK_SIZE = 100     # tickers per download request


def yahoo_symbol(ticker: str) -> str:
    """Yahoo writes share classes with a dash: BRK.B -> BRK-B."""
    return ticker.replace(".", "-")


def pct(new: float, old: float | None) -> float | None:
    if old is None or pd.isna(old) or old == 0:
        return None
    return round((new / old - 1) * 100, 2)


class YFinancePriceConnector(Connector):
    name = "yfinance"

    def __init__(self, tickers: list[str] | None = None, include_benchmarks: bool = True,
                 raw_dir: Path = RAW_DIR / "yfinance"):
        super().__init__(tickers)
        self.include_benchmarks = include_benchmarks
        self.raw_dir = Path(raw_dir)
        logging.getLogger("yfinance").setLevel(logging.WARNING)

    def _download(self, symbols: list[str], start: date, end: date) -> dict[str, pd.DataFrame]:
        frames: dict[str, pd.DataFrame] = {}
        for i in range(0, len(symbols), CHUNK_SIZE):
            chunk = symbols[i:i + CHUNK_SIZE]
            try:
                data = yf.download(chunk, start=start.isoformat(), end=end.isoformat(), interval="1d",
                                   auto_adjust=False, group_by="ticker", progress=False, threads=True)
            except Exception as exc:
                msg = f"download failed for {', '.join(chunk[:5])}{'...' if len(chunk) > 5 else ''}: {exc}"
                log.error(msg)
                self.errors.append(msg)
                continue
            for sym in chunk:
                if isinstance(data.columns, pd.MultiIndex):
                    if sym not in data.columns.get_level_values(0):
                        continue
                    df = data[sym]
                else:
                    df = data
                df = df.dropna(subset=["Close"])
                if not df.empty:
                    frames[sym] = df
        return frames

    def fetch(self, day: date) -> list[Item]:
        day_str = day.isoformat()
        start = day - timedelta(days=HISTORY_DAYS)
        end = day + timedelta(days=1)  # yfinance's end date is exclusive

        wanted = {yahoo_symbol(t): t for t in self.tickers}
        if self.include_benchmarks:
            wanted.update({sym: sym for sym in BENCHMARKS})
        log.info("Downloading %d symbol(s), %s to %s", len(wanted), start, day_str)
        frames = self._download(list(wanted), start, end)

        now_ny = datetime.now(NEW_YORK)
        items: list[Item] = []
        for sym, ticker in wanted.items():
            df = frames.get(sym)
            if df is None:
                msg = f"{ticker}: no price data (delisted, wrong ticker, or Yahoo blocked the request)"
                log.warning(msg)
                self.errors.append(msg)
                continue

            path = self.raw_dir / day_str / "prices" / f"{sym.replace('^', '_')}.csv"
            path.parent.mkdir(parents=True, exist_ok=True)
            df.to_csv(path)

            df = df[df.index.date <= day]
            if df.empty:
                continue
            closes, volumes = df["Close"], df["Volume"]
            bar_date = df.index[-1].date()
            close = float(closes.iloc[-1])

            def close_ago(n: int) -> float | None:
                return float(closes.iloc[-1 - n]) if len(closes) > n else None

            prior_year = closes[closes.index.year < bar_date.year]
            last_year_close = float(prior_year.iloc[-1]) if not prior_year.empty else None
            year = df[df.index.date > bar_date - timedelta(days=365)]
            high_52w, low_52w = float(year["High"].max()), float(year["Low"].min())
            avg_vol_20 = float(volumes.iloc[-21:-1].mean()) if len(volumes) > 1 else None
            volume = float(volumes.iloc[-1])
            partial = bar_date == now_ny.date() and (now_ny.hour, now_ny.minute) < (16, 0)

            change_1d = pct(close, close_ago(1))
            is_benchmark = sym in BENCHMARKS
            label = BENCHMARKS.get(sym, ticker)
            items.append(Item(
                source=self.name,
                source_id=f"{ticker}:{bar_date}",
                tickers=[] if is_benchmark else [ticker],
                title=(f"{label} closed {close:,.2f} on {bar_date}"
                       + (f" ({change_1d:+.2f}%)" if change_1d is not None else "")
                       + (" [market still open]" if partial else "")),
                url=f"https://finance.yahoo.com/quote/{sym}",
                published_at=bar_date.isoformat(),
                item_type="benchmark_daily" if is_benchmark else "price_daily",
                extra={
                    "raw_path": str(path),
                    "symbol": sym,
                    "date": bar_date.isoformat(),
                    "open": round(float(df["Open"].iloc[-1]), 4),
                    "high": round(float(df["High"].iloc[-1]), 4),
                    "low": round(float(df["Low"].iloc[-1]), 4),
                    "close": round(close, 4),
                    "adj_close": round(float(df["Adj Close"].iloc[-1]), 4),
                    "volume": int(volume),
                    "change_1d_pct": change_1d,
                    "change_5d_pct": pct(close, close_ago(5)),
                    "change_1m_pct": pct(close, close_ago(21)),
                    "change_ytd_pct": pct(close, last_year_close),
                    "high_52w": round(high_52w, 4),
                    "low_52w": round(low_52w, 4),
                    "from_52w_high_pct": pct(close, high_52w),
                    "from_52w_low_pct": pct(close, low_52w),
                    "avg_volume_20d": int(avg_vol_20) if avg_vol_20 and not pd.isna(avg_vol_20) else None,
                    "volume_vs_20d_avg": round(volume / avg_vol_20, 2) if avg_vol_20 else None,
                    "is_partial_session": partial,
                    "is_latest_fallback": bar_date.isoformat() != day_str,  # weekend/holiday: last trading day
                },
            ))
        return items


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch daily prices from yfinance for the watchlist")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD or 'today' (default): latest trading day on or before this day")
    parser.add_argument("--no-benchmarks", action="store_true", help="Skip the S&P 500 and Nasdaq-100")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)

    connector = YFinancePriceConnector(include_benchmarks=not args.no_benchmarks)
    items = connector.fetch(day)

    out = connector.raw_dir / day.isoformat() / "items.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([it.to_dict() for it in items], indent=2), encoding="utf-8")

    def f(v: float | None) -> str:
        return f"{v:+.2f}%" if v is not None else "n/a"

    print(f"\n{len(items)} symbol(s), latest trading day on or before {day} (! = market still open, numbers will change)")
    print(f"  {'symbol':<12}{'date':<12}{'close':>11}{'1d':>9}{'5d':>9}{'1m':>9}{'YTD':>9}{'vs 52w hi':>11}{'vol/avg':>9}")
    for it in items:
        e = it.extra
        mark = "!" if e["is_partial_session"] else " "
        name = it.tickers[0] if it.tickers else BENCHMARKS[e["symbol"]]
        print(f" {mark}{name:<12}{e['date']:<12}{e['close']:>11,.2f}{f(e['change_1d_pct']):>9}{f(e['change_5d_pct']):>9}"
              f"{f(e['change_1m_pct']):>9}{f(e['change_ytd_pct']):>9}{f(e['from_52w_high_pct']):>11}"
              f"{(str(e['volume_vs_20d_avg']) + 'x') if e['volume_vs_20d_avg'] else 'n/a':>9}")
    if connector.errors:
        print(f"\n{len(connector.errors)} error(s):")
        for err in connector.errors:
            print("  " + err)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
