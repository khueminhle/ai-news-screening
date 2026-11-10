"""SEC company facts: the official numbers each company tags in its 10-K / 10-Q (XBRL), one file per company.

    https://data.sec.gov/api/xbrl/companyfacts/CIK##########.json

Raw file -> data/raw/sec_facts/<day>/CIK##########.json. The database loader (storage/db.py) puts every
fact into the `financials` table exactly as SEC reports it: nothing is derived (no Q4 = year - Q1..Q3).

Limits worth knowing:
  - Only 10-K / 10-Q numbers. An earnings press release (8-K, EX-99.1) is not tagged, so brand-new
    results appear here only once the 10-Q / 10-K is filed (days to weeks later).
  - fy / fp in the SEC file describe the FILING, not the fact (a 10-Q also reports last year's quarter).
    Use period_start / period_end to know which period a number is for.

    python -m connectors.sec_facts
"""
from __future__ import annotations

import argparse
import json
import sys
from datetime import date
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import RAW_DIR, Item, utc_now_z
from connectors.sec import SecEdgarConnector

FACTS_URL = "https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json"


class SecFactsConnector(SecEdgarConnector):
    name = "sec_facts"

    def __init__(self, tickers: list[str] | None = None, raw_dir: Path = RAW_DIR / "sec_facts", **kwargs):
        super().__init__(tickers, raw_dir=raw_dir, download_documents=False, **kwargs)

    def fetch(self, day: date) -> list[Item]:
        items = []
        for cik, tickers in self.resolve_ciks().items():
            try:
                resp = self._get(FACTS_URL.format(cik=cik))
            except Exception as exc:
                self.errors.append(f"{tickers}: company facts not available ({exc})")
                continue
            path = self._save_raw(f"{day.isoformat()}/CIK{cik:010d}.json", resp.content)
            data = resp.json()
            facts = data.get("facts", {})
            n = sum(len(units) for concepts in facts.values() for c in concepts.values() for units in [c.get("units", {})])
            items.append(Item(
                source=self.name, source_id=f"CIK{cik:010d}", tickers=tickers,
                title=f"{data.get('entityName')} company facts", url=FACTS_URL.format(cik=cik),
                published_at=utc_now_z(), item_type="company_facts",
                extra={"cik": cik, "company_name": data.get("entityName"), "raw_path": str(path),
                       "taxonomies": sorted(facts), "concepts": sum(len(c) for c in facts.values()),
                       "concept_units": n}))
        return items


def iter_facts(data: dict):
    """Yield one dict per reported value, exactly as in the SEC file."""
    for taxonomy, concepts in data.get("facts", {}).items():
        for concept, body in concepts.items():
            for unit, rows in body.get("units", {}).items():
                for r in rows:
                    if r.get("val") is None or not r.get("end") or not r.get("accn"):
                        continue
                    yield {"taxonomy": taxonomy, "concept": concept, "label": body.get("label"), "unit": unit,
                           "period_start": r.get("start") or "", "period_end": r["end"], "value": r["val"],
                           "fiscal_year": r.get("fy"), "fiscal_period": r.get("fp"), "form": r.get("form"),
                           "accession_no": r["accn"], "filed_date": r.get("filed"), "frame": r.get("frame")}


def main() -> None:
    parser = argparse.ArgumentParser(description="Download SEC company facts for the watchlist")
    parser.add_argument("--day", default="today")
    args = parser.parse_args()
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)
    c = SecFactsConnector()
    for it in c.fetch(day):
        print(f"{','.join(it.tickers):<6} {it.extra['company_name']:<32} concepts={it.extra['concepts']:<5} -> {it.extra['raw_path']}")
    for e in c.errors:
        print("  !", e)


if __name__ == "__main__":
    main()
