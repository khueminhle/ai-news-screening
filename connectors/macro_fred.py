"""FRED connector: US macro data series (rates, yields, inflation, jobs, growth, markets).

Downloads every series listed in config/fred_series.json and, for each, returns
the latest value on or before --day with:
  - the previous observation and the change since then
  - the value about one year earlier and the 1-year change
Changes are worked out here in Python, never by the model.
  - For series measured in percent (yields, unemployment): change in percentage points.
  - For everything else (CPI, payrolls, oil): absolute change and % change.

Run it:
    python -m connectors.macro_fred
    python -m connectors.macro_fred --day 2026-10-31

Needs FRED_API in .env. FRED allows 120 requests a minute; each series uses 2.
The key has to go in the URL, so error messages are scrubbed before logging.
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from dotenv import load_dotenv

if __package__ in (None, ""):  # run as a plain script (e.g. VS Code Run button): make the project root importable
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import PROJECT_ROOT, RAW_DIR, Connector, Item

log = logging.getLogger("connectors.fred")

BASE_URL = "https://api.stlouisfed.org/fred"
SERIES_CONFIG = PROJECT_ROOT / "config" / "fred_series.json"
HISTORY_DAYS = 800  # enough for a 1-year comparison even for monthly/quarterly series published months late


def load_series_config(path: Path = SERIES_CONFIG) -> list[dict]:
    data = json.loads(path.read_text(encoding="utf-8"))
    series = data.get("series") if isinstance(data, dict) else data
    if not series:
        raise ValueError(f"{path} must contain a non-empty 'series' list")
    return [{"id": s["id"].strip().upper(), "group": s.get("group", "other"), "label": s.get("label") or s["id"]}
            for s in series]


class FredConnector(Connector):
    name = "fred"

    def __init__(self, series: list[dict] | None = None, raw_dir: Path = RAW_DIR / "fred",
                 seconds_between_calls: float = 0.6):
        super().__init__(tickers=[])  # macro source: not tied to the watchlist
        load_dotenv()
        self.api_key = (os.environ.get("FRED_API") or os.environ.get("FRED_API_KEY") or "").strip()
        if not self.api_key:
            raise RuntimeError("FRED_API is not set. Add 'FRED_API=your_key' to .env")
        self.series = series if series is not None else load_series_config()
        self.raw_dir = Path(raw_dir)
        self.min_interval = seconds_between_calls
        self.session = requests.Session()
        self._last_request = 0.0

    def _scrub(self, text: str) -> str:
        return text.replace(self.api_key, "***")

    def _get(self, path: str, params: dict, retries: int = 4) -> dict:
        params = {**params, "api_key": self.api_key, "file_type": "json"}
        last_error = ""
        for attempt in range(retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self.session.get(BASE_URL + path, params=params, timeout=30)
                if resp.status_code == 200:
                    return resp.json()
                try:
                    detail = resp.json().get("error_message", "")
                except ValueError:
                    detail = ""
                last_error = f"HTTP {resp.status_code} {detail}".strip()
                if resp.status_code in (400, 401, 403, 404):
                    break
            except requests.RequestException as exc:
                last_error = self._scrub(str(exc))  # exceptions can contain the URL, which contains the key
            if attempt < retries:
                log.warning("Request failed (%s). Retrying in %ss", last_error, 2 ** attempt)
                time.sleep(2 ** attempt)
        raise RuntimeError(self._scrub(last_error))

    @staticmethod
    def _closest_on_or_before(obs: list[tuple[str, float]], target: str) -> tuple[str, float] | None:
        earlier = [o for o in obs if o[0] <= target]
        return earlier[-1] if earlier else None

    def fetch(self, day: date) -> list[Item]:
        day_str = day.isoformat()
        start = (day - timedelta(days=HISTORY_DAYS)).isoformat()
        items: list[Item] = []

        for n, s in enumerate(self.series, start=1):
            sid = s["id"]
            try:
                meta = self._get("/series", {"series_id": sid})["seriess"][0]
                data = self._get("/series/observations",
                                 {"series_id": sid, "observation_start": start, "observation_end": day_str})
            except Exception as exc:
                msg = f"{sid}: {exc}"
                log.error(msg)
                self.errors.append(msg)
                continue

            path = self.raw_dir / day_str / "series" / f"{sid}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"series": meta, "observations": data.get("observations", [])}, indent=1),
                            encoding="utf-8")

            # "." means no value that day (e.g. a market holiday)
            obs = [(o["date"], float(o["value"])) for o in data.get("observations", []) if o["value"] not in (".", "")]
            if not obs:
                msg = f"{sid}: no observations between {start} and {day_str}"
                log.warning(msg)
                self.errors.append(msg)
                continue

            latest_date, latest = obs[-1]
            prev = obs[-2] if len(obs) > 1 else None
            year_ago = self._closest_on_or_before(obs, (date.fromisoformat(latest_date) - timedelta(days=365)).isoformat())
            units = meta.get("units", "")
            is_percent = units.lower().startswith("percent")

            def change(old: tuple[str, float] | None) -> dict:
                if not old:
                    return {"date": None, "value": None, "change": None, "change_pct": None}
                diff = round(latest - old[1], 4)
                pct = None if is_percent or old[1] == 0 else round((latest / old[1] - 1) * 100, 2)
                return {"date": old[0], "value": old[1], "change": diff, "change_pct": pct}

            prev_c, year_c = change(prev), change(year_ago)
            items.append(Item(
                source=self.name,
                source_id=f"{sid}:{latest_date}",
                tickers=[],
                title=f"{s['label']}: {latest:g} ({units_short(units)}, {latest_date})",
                url=f"https://fred.stlouisfed.org/series/{sid}",
                published_at=latest_date,
                item_type="macro_series",
                extra={
                    "raw_path": str(path),
                    "series_id": sid,
                    "group": s["group"],
                    "label": s["label"],
                    "fred_title": meta.get("title"),
                    "units": units,
                    "change_unit": "percentage points" if is_percent else units,
                    "frequency": meta.get("frequency"),
                    "latest_date": latest_date,
                    "latest_value": latest,
                    "previous": prev_c,
                    "year_ago": year_c,
                    "fred_last_updated": meta.get("last_updated"),
                },
            ))
            log.info("[%d/%d] %s = %g on %s", n, len(self.series), sid, latest, latest_date)
        return items


def units_short(units: str) -> str:
    u = units.lower()
    if u.startswith("percent"):
        return "%"
    if u.startswith("index"):
        return "index"
    return units


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch FRED macro series listed in config/fred_series.json")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD or 'today' (default): latest value on or before this day")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)

    connector = FredConnector()
    items = connector.fetch(day)

    out = connector.raw_dir / day.isoformat() / "items.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps([it.to_dict() for it in items], indent=2), encoding="utf-8")

    def fmt(c: dict, pp: bool) -> str:
        if c["change"] is None:
            return "n/a"
        return f"{c['change']:+.2f} pp" if pp else f"{c['change']:+,.2f} ({c['change_pct']:+.2f}%)"

    print(f"\n{len(items)} of {len(connector.series)} series, latest value on or before {day}")
    group = None
    for it in sorted(items, key=lambda i: i.extra["group"]):
        e = it.extra
        if e["group"] != group:
            group = e["group"]
            print(f"\n  [{group}]")
        pp = e["change_unit"] == "percentage points"
        print(f"  {e['series_id']:<13} {e['latest_value']:>12,.3f}  {e['latest_date']}  "
              f"vs prev: {fmt(e['previous'], pp):<22} vs 1y: {fmt(e['year_ago'], pp):<22} {e['label']}")
    if connector.errors:
        print(f"\n{len(connector.errors)} error(s):")
        for err in connector.errors:
            print("  " + err)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
