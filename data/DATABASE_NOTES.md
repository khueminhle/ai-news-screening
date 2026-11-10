# signals.db: notes

**Last updated:** 2026-11-10. Full details: `mission-control.md`, section 5.

## Not yet verified with real data

These fields follow rules that are built and tested offline, but the real-world situation has not happened yet. Until it does, treat them as **unverified**:

| Table / field | What should happen | Status |
|---|---|---|
| `earnings_calendar`: `eps_estimate_prev1/prev2`, `revenue_estimate_prev1/prev2`, `report_date_prev1/prev2` (+ `_seen`) | Filled when Finnhub changes an estimate or date | **Empty**: no change seen yet |
| `earnings_changes` (whole table) | One row per change | **Empty**: no change seen yet |
| `macro_series`: `revised_from`, `revised_at` | Filled when FRED revises a past value | **Empty**: no revision seen yet |
| `prices`: `adj_close` on past days | Updated after a dividend or split (change > 0.02%) | **Not yet seen** |
| `document_sections`: `kind = 'table'` rows | Tables kept as rows, numbers under the right columns | **Only Ubiquiti's press release checked by eye**; other documents not reviewed |
| `items.event_id` / `events` | Copies of the same story linked to one event | **Works for identical headlines only**; differently worded copies stay separate by design |
| `filings.accepted_at`, `items.published_at` for SEC | EDGAR's `...Z` times are real UTC | **Confirmed on one filing only** |
| `item_labels` (Claude, M5) | Correct relevance, tickers, importance; `near_duplicate_of` links true copies | **First run (206 items) checked by eye only.** No clear miss found yet (item 31 tagged AMD is fine: headline says "AMD Rises 4%") |
| `financials` (SEC company facts, M5) | Every value exactly as SEC reports it | **Checked for Ubiquiti FQ1 FY27 only** (revenue, EPS, net income, gross profit match the press release) |
| `llm_quarantine` | Items whose Claude output failed twice | **Empty**: tested offline only, no real failure yet |
| `digests`, `digest_lines` (M6) | Kept bullets are correct; removed bullets listed with the reason | **First digest (2026-11-10) not yet read by the owner.** Numbers pass check 1; wording is not checked by code (one known misstatement in the UI section) |

`financials.fiscal_year` / `fiscal_period` belong to the filing the number came from, not the number's own period. Use `period_start` / `period_end`. `period_start = ''` means a point-in-time value (balance sheet).

When one of these happens and looks right, move it to "Verified with real data" in `mission-control.md`.

## Do not

- Delete `signals.db-wal` or `signals.db-shm` while they exist (recent data may still be in the `-wal` file).
- Delete anything in `raw/`: the database is rebuilt and re-processed from it.

## Useful commands

```
python -m storage.inspect_db                # structure, duplicate checks, latest runs
python -m storage.inspect_db --duplicates
python -m pipeline.run_fetch                # daily fetch into this database (includes sec_facts)
python -m pipeline.run_classify             # Claude labels new items (after the fetch)
python -m pipeline.run_digest               # daily digest -> data/digests/<day>/digest.md (after classify)
python -m pipeline.run_dashboard            # dashboard -> data/dashboards/<day>/dashboard.html (after the digest)
```

- Delete nothing in `llm/` either: it holds exactly what Claude was sent and what it replied, per batch.
