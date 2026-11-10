"""SEC EDGAR connector: filings for the watchlist, with the full text of every document.

For each ticker in watchlist/watchlist.json it finds 8-K, 10-Q and 10-K filings (and
amendments), then for each filing:
  1. reads the filing's official index (the list of every file and its type);
  2. records every file, and downloads every TEXT document: the main document and
     all exhibits (EX-99 press releases, EX-10 contracts, EX-21, EX-31/32 ...), including PDFs;
     images, XBRL data files, viewer pages and the duplicate full-submission file are
     recorded but not downloaded;
  3. extracts clean text with tables kept as rows, and splits 10-K / 10-Q / 8-K main
     documents by "Item" section (see rag/parse.py).

Which filings:
  - normal run: filings on --day; for a company with none that day, its latest earlier
    filing date (marked extra["is_latest_fallback"] = True)
  - --backfill-years N: every filing from the last N years (one-off, at the start)

Files are downloaded once and reused on later runs:
    data/raw/sec/documents/<TICKER>/<accession>/<file>               original
    data/raw/sec/documents/<TICKER>/<accession>/<file>.blocks.json   extracted text + sections
    data/raw/sec/documents/<TICKER>/<accession>/_index.html          the filing index

Run it:
    python -m connectors.sec
    python -m connectors.sec --day 2026-11-06
    python -m connectors.sec --backfill-years 2
    python -m connectors.sec --list-only          # filings only, no documents

Needs SEC_USER_AGENT in .env, e.g.  SEC_USER_AGENT=Your Name you@example.com
"""
from __future__ import annotations

import argparse
import json
import logging
import os
import re
import time
from datetime import date, timedelta
from pathlib import Path

import requests
from bs4 import BeautifulSoup
from dotenv import load_dotenv

if __package__ in (None, ""):  # run as a plain script (e.g. VS Code Run button): make the project root importable
    import sys
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from connectors.base import RAW_DIR, Connector, Item, write_items_json
from rag.parse import parse_document

log = logging.getLogger("connectors.sec")

TICKER_MAP_URL = "https://www.sec.gov/files/company_tickers.json"
SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik:010d}.json"
OLDER_SUBMISSIONS_URL = "https://data.sec.gov/submissions/{name}"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
INDEX_URL = ARCHIVE_URL + "{accession}-index.html"

DEFAULT_FORMS = {"8-K", "8-K/A", "10-Q", "10-Q/A", "10-K", "10-K/A"}
SEC_MAX_REQUESTS_PER_SECOND = 10
TEXT_EXTENSIONS = {".htm", ".html", ".txt", ".pdf"}


def skip_reason(file_name: str, doc_type: str, description: str, accession: str) -> str | None:
    """Why a file is recorded but not downloaded; None = download it."""
    name = file_name.lower()
    if Path(name).suffix not in TEXT_EXTENSIONS:
        return "not a text document (image, data or display file)"
    if name == f"{accession}.txt" or "complete submission" in (description or "").lower():
        return "duplicate: the whole filing in one file"
    if re.fullmatch(r"r\d+\.htm", name):
        return "XBRL viewer page"
    if (doc_type or "").upper().startswith("EX-101"):
        return "XBRL data file"
    return None


def columns_to_rows(cols: dict) -> list[dict]:
    """EDGAR stores filings as parallel lists; turn them into one dict per filing."""
    keys = list(cols)
    return [{k: cols[k][i] for k in keys} for i in range(len(cols.get("accessionNumber", [])))]


class SecEdgarConnector(Connector):
    name = "sec_edgar"

    def __init__(
        self,
        tickers: list[str] | None = None,
        forms: set[str] | None = None,
        download_documents: bool = True,
        backfill_years: int | None = None,
        requests_per_second: float = 8,
        raw_dir: Path = RAW_DIR / "sec",
        fallback_to_latest: bool = True,
    ):
        super().__init__(tickers)
        load_dotenv()
        user_agent = os.environ.get("SEC_USER_AGENT", "").strip()
        if not user_agent:
            raise RuntimeError("SEC_USER_AGENT is not set. Add 'SEC_USER_AGENT=Your Name you@example.com' to .env")

        self.session = requests.Session()
        self.session.headers.update({"User-Agent": user_agent, "Accept-Encoding": "gzip, deflate"})
        self.forms = forms or DEFAULT_FORMS
        self.download_documents = download_documents
        self.backfill_years = backfill_years
        self.fallback_to_latest = fallback_to_latest
        self.raw_dir = Path(raw_dir)
        self.min_interval = 1.0 / min(requests_per_second, SEC_MAX_REQUESTS_PER_SECOND)
        self._last_request = 0.0
        self.stats = {"filings": 0, "files_recorded": 0, "documents_downloaded": 0, "documents_reused": 0,
                      "documents_unsplit": 0}

    # ---------- HTTP ----------

    def _get(self, url: str, retries: int = 4) -> requests.Response:
        """GET with rate limiting and exponential back-off. 404 is not retried."""
        last_error: Exception | None = None
        for attempt in range(retries + 1):
            wait = self.min_interval - (time.monotonic() - self._last_request)
            if wait > 0:
                time.sleep(wait)
            self._last_request = time.monotonic()
            try:
                resp = self.session.get(url, timeout=60)
                if resp.status_code == 200:
                    return resp
                if resp.status_code == 404:
                    resp.raise_for_status()
                last_error = requests.HTTPError(f"HTTP {resp.status_code} for {url}", response=resp)
            except requests.HTTPError:
                raise
            except requests.RequestException as exc:
                last_error = exc
            if attempt < retries:
                backoff = 2 ** attempt
                log.warning("Request failed (%s). Retrying in %ss", last_error, backoff)
                time.sleep(backoff)
        raise last_error  # type: ignore[misc]

    def _save_raw(self, relative_path: str, content: bytes) -> Path:
        path = self.raw_dir / relative_path
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return path

    # ---------- CIK lookup ----------

    def resolve_ciks(self) -> dict[int, list[str]]:
        """Map CIK -> watchlist tickers. Tickers that share a CIK (e.g. GOOG/GOOGL) are grouped."""
        resp = self._get(TICKER_MAP_URL)
        self._save_raw("company_tickers.json", resp.content)
        sec_map = {row["ticker"].upper(): int(row["cik_str"]) for row in resp.json().values()}

        ciks: dict[int, list[str]] = {}
        for ticker in self.tickers:
            cik = sec_map.get(ticker) or sec_map.get(ticker.replace(".", "-"))
            if cik is None:
                msg = f"{ticker}: not found in SEC ticker list (not an SEC filer, or a typo)"
                log.warning(msg)
                self.errors.append(msg)
                continue
            ciks.setdefault(cik, []).append(ticker)
        return ciks

    # ---------- which filings ----------

    def _all_filings(self, data: dict, since: str | None) -> list[dict]:
        rows = columns_to_rows(data["filings"]["recent"])
        # "recent" holds about the last 1,000 filings; a backfill may need the older pages too.
        if since and (not rows or min(r["filingDate"] for r in rows) > since):
            for page in data["filings"].get("files", []):
                if page.get("filingTo", "") >= since:
                    older = self._get(OLDER_SUBMISSIONS_URL.format(name=page["name"])).json()
                    rows += columns_to_rows(older)
        return rows

    def _choose(self, rows: list[dict], day_str: str) -> tuple[list[dict], bool]:
        wanted = [r for r in rows if r["form"] in self.forms and r["filingDate"] <= day_str]
        if self.backfill_years:
            since = (date.fromisoformat(day_str) - timedelta(days=365 * self.backfill_years)).isoformat()
            return [r for r in wanted if r["filingDate"] >= since], False
        on_day = [r for r in wanted if r["filingDate"] == day_str]
        if on_day or not self.fallback_to_latest:
            return on_day, False
        latest = max((r["filingDate"] for r in wanted), default=None)
        return [r for r in wanted if r["filingDate"] == latest], True

    # ---------- fetch ----------

    def fetch(self, day: date) -> list[Item]:
        day_str = day.isoformat()
        since = (day - timedelta(days=365 * self.backfill_years)).isoformat() if self.backfill_years else None
        ciks = self.resolve_ciks()
        log.info("Checking %d companies (%s)", len(ciks),
                 f"backfill {since} to {day_str}" if since else f"filings on {day_str}, or each company's latest")

        items: list[Item] = []
        for n, (cik, tickers) in enumerate(ciks.items(), start=1):
            try:
                resp = self._get(SUBMISSIONS_URL.format(cik=cik))
                self._save_raw(f"{day_str}/submissions/CIK{cik:010d}.json", resp.content)
                data = resp.json()
                chosen, is_fallback = self._choose(self._all_filings(data, since), day_str)
                log.info("[%d/%d] %s: %d filing(s)", n, len(ciks), ",".join(tickers), len(chosen))
                for row in chosen:
                    try:
                        items.append(self._process_filing(cik, tickers, data["name"], row, is_fallback))
                    except Exception as exc:  # one bad filing never stops the rest
                        msg = f"{','.join(tickers)} {row['accessionNumber']}: {exc}"
                        log.error(msg)
                        self.errors.append(msg)
            except Exception as exc:  # one failed company never stops the rest
                msg = f"{','.join(tickers)} (CIK {cik}): {exc}"
                log.error(msg)
                self.errors.append(msg)
        return items

    def _process_filing(self, cik: int, tickers: list[str], company: str, row: dict, is_fallback: bool) -> Item:
        accession, form = row["accessionNumber"], row["form"]
        acc_nodash = accession.replace("-", "")
        index_url = INDEX_URL.format(cik=cik, acc=acc_nodash, accession=accession)
        folder = self.raw_dir / "documents" / tickers[0] / acc_nodash
        primary = row.get("primaryDocument") or ""

        files = self._filing_files(cik, accession, index_url, folder) if self.download_documents else []
        self.stats["filings"] += 1
        self.stats["files_recorded"] += len(files)

        doc_url = (ARCHIVE_URL.format(cik=cik, acc=acc_nodash) + primary) if primary else index_url
        description = row.get("primaryDocDescription") or ""
        return Item(
            source=self.name,
            source_id=accession,
            tickers=tickers,
            title=f"{company} {form}" + (f": {description}" if description and description != form else ""),
            url=doc_url,
            published_at=row["filingDate"],
            item_type=form,
            extra={
                "cik": cik,
                "company_name": company,
                "filed_date": row["filingDate"],
                "report_date": row.get("reportDate"),
                "acceptance_datetime": row.get("acceptanceDateTime"),
                "items": row.get("items"),  # 8-K item codes, e.g. "2.02,9.01"
                "primary_document": primary,
                "index_url": index_url,
                "files": files,
                "is_latest_fallback": is_fallback,
                "is_backfill": bool(self.backfill_years),
            },
        )

    def _filing_files(self, cik: int, accession: str, index_url: str, folder: Path) -> list[dict]:
        """Record every file in the filing; download and extract the text documents."""
        index_path = folder / "_index.html"
        if index_path.exists():
            index_html = index_path.read_bytes()
        else:
            index_html = self._get(index_url).content
            folder.mkdir(parents=True, exist_ok=True)
            index_path.write_bytes(index_html)

        soup = BeautifulSoup(index_html, "html.parser")
        files = []
        for table in soup.select("table.tableFile"):
            for tr in table.find_all("tr")[1:]:
                tds = tr.find_all("td")
                link = tr.find("a")
                if len(tds) < 4 or not link:
                    continue
                file_name = link.get_text(strip=True)
                doc_type = tds[3].get_text(strip=True)
                description = tds[1].get_text(strip=True)
                size_text = tds[4].get_text(strip=True) if len(tds) > 4 else ""
                reason = skip_reason(file_name, doc_type, description, accession)
                entry = {"file_name": file_name, "doc_type": doc_type or None, "description": description or None,
                         "size_bytes": int(size_text) if size_text.isdigit() else None,
                         "downloaded": False, "skip_reason": reason, "local_path": None, "blocks_path": None}
                if reason is None:
                    try:
                        entry.update(self._download_and_extract(cik, accession, folder, file_name, doc_type))
                    except Exception as exc:
                        entry["skip_reason"] = f"download/extract failed: {exc}"
                        self.errors.append(f"{accession}/{file_name}: {exc}")
                files.append(entry)
        return files

    def _download_and_extract(self, cik: int, accession: str, folder: Path, file_name: str, doc_type: str) -> dict:
        path = folder / file_name
        blocks_path = folder / f"{file_name}.blocks.json"
        if path.exists():
            self.stats["documents_reused"] += 1
        else:
            url = ARCHIVE_URL.format(cik=cik, acc=accession.replace("-", "")) + file_name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(self._get(url).content)
            self.stats["documents_downloaded"] += 1
        if not blocks_path.exists():
            parsed = parse_document(path, doc_type)
            blocks_path.write_text(json.dumps(parsed, ensure_ascii=False), encoding="utf-8")
            if parsed["sections_found"] is False:
                self.stats["documents_unsplit"] += 1
                self.errors.append(f"{accession}/{file_name}: {'; '.join(parsed['warnings'])}")
        return {"downloaded": True, "local_path": str(path), "blocks_path": str(blocks_path)}


def main() -> None:
    parser = argparse.ArgumentParser(description="Fetch SEC EDGAR filings (with full documents) for the watchlist")
    parser.add_argument("--day", default="today", help="YYYY-MM-DD or 'today' (default)")
    parser.add_argument("--forms", nargs="*", help="Form types to keep (default: 8-K, 10-Q, 10-K and amendments)")
    parser.add_argument("--backfill-years", type=int, help="Fetch every filing from the last N years (one-off)")
    parser.add_argument("--list-only", action="store_true", help="List filings only; do not download documents")
    parser.add_argument("--no-fallback", action="store_true",
                        help="Only filings from --day; no fallback to each company's latest earlier filing")
    args = parser.parse_args()

    logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"), format="%(asctime)s %(levelname)s %(message)s")
    day = date.today() if args.day == "today" else date.fromisoformat(args.day)

    connector = SecEdgarConnector(
        forms=set(args.forms) if args.forms else None,
        download_documents=not args.list_only,
        backfill_years=args.backfill_years,
        fallback_to_latest=not args.no_fallback,
    )
    items = connector.fetch(day)
    out = write_items_json(connector.raw_dir, day, items)

    print(f"\n{len(items)} filing(s) (* = no filing that day, showing the latest earlier one)")
    for it in items:
        mark = "*" if it.extra.get("is_latest_fallback") else " "
        got = [f for f in it.extra["files"] if f["downloaded"]]
        types = ", ".join(sorted({f["doc_type"] or "?" for f in got}))
        print(f" {mark}{','.join(it.tickers):<6} {it.published_at}  {it.item_type:<7} {it.source_id}  "
              f"{len(got)}/{len(it.extra['files'])} files downloaded  [{types}]")
    print("\n" + ", ".join(f"{k}: {v}" for k, v in connector.stats.items()))
    if connector.errors:
        print(f"\n{len(connector.errors)} warning(s)/error(s):")
        for err in connector.errors[:30]:
            print("  " + err)
    print(f"\nSaved: {out}")


if __name__ == "__main__":
    main()
