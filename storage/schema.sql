-- Investment Signal Agent: SQLite schema (Milestones 1-7). Postgres + pgvector from Milestone 8.
-- Time rules: moments are 'YYYY-MM-DDTHH:MM:SSZ' (UTC); calendar dates are 'YYYY-MM-DD'.
-- Every table has a unique key, so running a fetch twice never creates duplicates.

PRAGMA foreign_keys = ON;

-- One row per connector run.
CREATE TABLE IF NOT EXISTS fetch_runs (
    id            INTEGER PRIMARY KEY,
    connector     TEXT NOT NULL,
    run_day       TEXT NOT NULL,              -- the --day the run was for (date at time of fetch by default)
    started_at    TEXT NOT NULL,
    finished_at   TEXT,
    status        TEXT,                       -- ok | degraded (some errors) | failed
    counts        TEXT,                       -- JSON: rows added / updated per table
    errors        TEXT                        -- JSON list
);

-- Full texts: SEC main documents and exhibits, Fed releases.
CREATE TABLE IF NOT EXISTS documents (
    id              INTEGER PRIMARY KEY,
    source          TEXT NOT NULL,            -- sec_edgar | federal_reserve
    source_ref      TEXT NOT NULL,            -- SEC: accession/file name; Fed: release id
    doc_type        TEXT,                     -- 10-K, 8-K, EX-99.1, fomc_statement, fed_speech ...
    title           TEXT,
    published_at    TEXT,
    full_text       TEXT,
    char_count      INTEGER,
    sections_found  INTEGER,                  -- 1 yes, 0 no (kept whole), NULL = not split by design
    warnings        TEXT,                     -- JSON list
    local_path      TEXT,                     -- raw file on disk (kept for re-processing)
    created_at      TEXT NOT NULL,
    UNIQUE (source, source_ref)
);

-- Sections and table blocks of each document, in order.
CREATE TABLE IF NOT EXISTS document_sections (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES documents(id),
    seq          INTEGER NOT NULL,
    section      TEXT,                        -- "Item 1A", "Part I Item 2", "Item 2.02", "EX-99.1", "Full text"
    heading      TEXT,
    kind         TEXT NOT NULL,               -- text | table
    content      TEXT NOT NULL,
    UNIQUE (document_id, seq)
);

-- Real-world stories: copies of the same story from different sources point to one event.
CREATE TABLE IF NOT EXISTS events (
    id                 INTEGER PRIMARY KEY,
    clean_title        TEXT NOT NULL,
    tickers            TEXT,                  -- comma-separated
    first_published_at TEXT,
    primary_item_id    INTEGER,
    created_at         TEXT NOT NULL
);

-- The short list: news, market news, SEC filings, Fed releases.
CREATE TABLE IF NOT EXISTS items (
    id             INTEGER PRIMARY KEY,
    source         TEXT NOT NULL,             -- finnhub | newsdata | sec_edgar | federal_reserve
    source_id      TEXT NOT NULL,
    item_type      TEXT NOT NULL,             -- news | market_news | 8-K | 10-Q | 10-K | fomc_statement | fed_speech ...
    title          TEXT,
    clean_title    TEXT,                      -- rule-based, used to group the same story
    summary        TEXT,                      -- publisher's teaser (news) or RSS description; NULL if none
    publisher      TEXT,
    url            TEXT,
    published_at   TEXT,
    sentiment      TEXT,                      -- NewsData's label, if any
    relevance      TEXT,                      -- 'relevant' | 'irrelevant', from the latest Claude label (Milestone 5)
    event_id       INTEGER REFERENCES events(id),
    document_id    INTEGER REFERENCES documents(id),
    extra          TEXT,                      -- JSON: remaining source-specific fields
    first_seen_at  TEXT NOT NULL,
    last_seen_at   TEXT NOT NULL,
    UNIQUE (source, source_id)
);

CREATE TABLE IF NOT EXISTS item_tickers (
    item_id  INTEGER NOT NULL REFERENCES items(id),
    ticker   TEXT NOT NULL,
    PRIMARY KEY (item_id, ticker)
);

-- SEC filings and every file inside them (downloaded or not).
CREATE TABLE IF NOT EXISTS filings (
    accession_no  TEXT PRIMARY KEY,
    cik           INTEGER NOT NULL,
    tickers       TEXT,
    company       TEXT,
    form          TEXT NOT NULL,
    filed_date    TEXT,
    report_date   TEXT,
    accepted_at   TEXT,
    items_8k      TEXT,
    index_url     TEXT,
    item_id       INTEGER REFERENCES items(id),
    created_at    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS filing_files (
    accession_no  TEXT NOT NULL REFERENCES filings(accession_no),
    file_name     TEXT NOT NULL,
    doc_type      TEXT,                       -- 10-K, EX-99.1, EX-31.1, GRAPHIC, EX-101.SCH ...
    description   TEXT,
    size_bytes    INTEGER,
    downloaded    INTEGER NOT NULL,           -- 1 = text document downloaded and stored, 0 = recorded only
    skip_reason   TEXT,
    local_path    TEXT,
    document_id   INTEGER REFERENCES documents(id),
    PRIMARY KEY (accession_no, file_name)
);

-- Daily prices. A bar from a session still in progress has is_final = 0 and is replaced later.
CREATE TABLE IF NOT EXISTS prices (
    ticker      TEXT NOT NULL,                -- watchlist ticker, or benchmark symbol (^GSPC, ^NDX)
    date        TEXT NOT NULL,
    open        REAL, high REAL, low REAL, close REAL, adj_close REAL,
    volume      INTEGER,
    is_final    INTEGER NOT NULL,
    source      TEXT NOT NULL,
    fetched_at  TEXT NOT NULL,
    PRIMARY KEY (ticker, date, source)
);

-- FRED.
CREATE TABLE IF NOT EXISTS series_info (
    series_id     TEXT PRIMARY KEY,
    label         TEXT,
    grp           TEXT,
    units         TEXT,
    frequency     TEXT,
    fred_title    TEXT,
    last_updated  TEXT
);

CREATE TABLE IF NOT EXISTS macro_series (
    series_id     TEXT NOT NULL,
    date          TEXT NOT NULL,
    value         REAL NOT NULL,
    revised_from  REAL,                       -- previous value if FRED revised this date
    revised_at    TEXT,
    source        TEXT NOT NULL DEFAULT 'fred',
    fetched_at    TEXT NOT NULL,
    PRIMARY KEY (series_id, date)
);

-- Analyst rating counts (third-party data; reported with attribution).
CREATE TABLE IF NOT EXISTS analyst_ratings (
    ticker       TEXT NOT NULL,
    period       TEXT NOT NULL,               -- first day of the month, e.g. 2026-11-01
    strong_buy   INTEGER, buy INTEGER, hold INTEGER, sell INTEGER, strong_sell INTEGER,
    total        INTEGER,
    source       TEXT NOT NULL,
    fetched_at   TEXT NOT NULL,
    PRIMARY KEY (ticker, period, source)
);

-- Earnings dates and estimates: current value plus the previous two, each with the date it was seen.
CREATE TABLE IF NOT EXISTS earnings_calendar (
    ticker                  TEXT NOT NULL,
    fiscal_year             INTEGER NOT NULL,
    fiscal_quarter          INTEGER NOT NULL,
    report_date             TEXT,  report_date_seen  TEXT,
    report_date_prev1       TEXT,  report_date_prev1_seen TEXT,
    report_date_prev2       TEXT,  report_date_prev2_seen TEXT,
    timing                  TEXT,                       -- before open | after close | NULL
    eps_estimate            REAL,  eps_estimate_seen TEXT,
    eps_estimate_prev1      REAL,  eps_estimate_prev1_seen TEXT,
    eps_estimate_prev2      REAL,  eps_estimate_prev2_seen TEXT,
    revenue_estimate        REAL,  revenue_estimate_seen TEXT,
    revenue_estimate_prev1  REAL,  revenue_estimate_prev1_seen TEXT,
    revenue_estimate_prev2  REAL,  revenue_estimate_prev2_seen TEXT,
    eps_actual              REAL,
    revenue_actual          REAL,
    source                  TEXT NOT NULL,
    first_seen_at           TEXT NOT NULL,
    updated_at              TEXT NOT NULL,
    PRIMARY KEY (ticker, fiscal_year, fiscal_quarter, source)
);

-- Every change to an earnings row, so nothing is lost beyond the last two values.
CREATE TABLE IF NOT EXISTS earnings_changes (
    id              INTEGER PRIMARY KEY,
    ticker          TEXT NOT NULL,
    fiscal_year     INTEGER NOT NULL,
    fiscal_quarter  INTEGER NOT NULL,
    field           TEXT NOT NULL,
    old_value       TEXT,
    new_value       TEXT,
    changed_at      TEXT NOT NULL
);

-- ---------- Milestone 5: Claude labels + SEC company facts ----------

-- One row per Claude call (like fetch_runs, for the classifier).
CREATE TABLE IF NOT EXISTS llm_runs (
    id              INTEGER PRIMARY KEY,
    task            TEXT NOT NULL,            -- classify
    run_day         TEXT NOT NULL,
    batch           TEXT NOT NULL,            -- e.g. UI_batch01, UI_batch01_retry
    model           TEXT NOT NULL,
    effort          TEXT,
    prompt_version  TEXT NOT NULL,
    item_count      INTEGER,
    status          TEXT,                     -- ok | partial (some items failed) | failed
    seconds         REAL,
    input_path      TEXT,
    output_path     TEXT,
    error           TEXT,
    usage           TEXT,                     -- JSON from the CLI (tokens), if given
    started_at      TEXT NOT NULL
);

-- Claude's labels for an item. A new prompt version adds rows; old labels are kept.
CREATE TABLE IF NOT EXISTS item_labels (
    id                 INTEGER PRIMARY KEY,
    item_id            INTEGER NOT NULL REFERENCES items(id),
    prompt_version     TEXT NOT NULL,
    model              TEXT NOT NULL,
    llm_run_id         INTEGER REFERENCES llm_runs(id),
    relevant           INTEGER NOT NULL,      -- 1 / 0
    tickers            TEXT,                  -- JSON list, watchlist tickers only
    event_type         TEXT NOT NULL,
    importance         TEXT NOT NULL,         -- high | medium | low
    importance_reason  TEXT,
    sentiment          TEXT,                  -- positive | negative | neutral | mixed (for the tickers)
    fiscal_period      TEXT,                  -- exactly as the item states it, e.g. "FQ1 2027"; never converted
    near_duplicate_of  INTEGER REFERENCES items(id),
    one_line           TEXT,
    labelled_at        TEXT NOT NULL,
    UNIQUE (item_id, prompt_version)
);

-- Numbers Claude found in an item; quote is checked to appear in the item's text.
CREATE TABLE IF NOT EXISTS item_figures (
    id        INTEGER PRIMARY KEY,
    label_id  INTEGER NOT NULL REFERENCES item_labels(id),
    item_id   INTEGER NOT NULL REFERENCES items(id),
    label     TEXT NOT NULL,                  -- "EPS", "revenue", "EPS estimate" ...
    value     TEXT NOT NULL,                  -- as written: "$5.32", "$1.026B"
    quote     TEXT NOT NULL
);

-- Items whose Claude output failed the checks twice. Kept for review, not used.
CREATE TABLE IF NOT EXISTS llm_quarantine (
    id              INTEGER PRIMARY KEY,
    item_id         INTEGER REFERENCES items(id),
    prompt_version  TEXT NOT NULL,
    llm_run_id      INTEGER REFERENCES llm_runs(id),
    reason          TEXT NOT NULL,
    output_path     TEXT,
    resolved        INTEGER NOT NULL DEFAULT 0,
    created_at      TEXT NOT NULL
);

-- SEC company facts (XBRL), stored exactly as SEC reports them: no derived numbers.
-- Note: fiscal_year / fiscal_period describe the FILING the fact came from, not the fact's own
-- period (a 10-Q also reports last year's quarter). Use period_start / period_end for the period.
CREATE TABLE IF NOT EXISTS financials (
    ticker         TEXT NOT NULL,
    cik            INTEGER NOT NULL,
    taxonomy       TEXT NOT NULL,             -- us-gaap | dei | ...
    concept        TEXT NOT NULL,             -- Revenues, EarningsPerShareDiluted ...
    label          TEXT,
    unit           TEXT NOT NULL,             -- USD, USD/shares, shares ...
    period_start   TEXT NOT NULL DEFAULT '',  -- '' for point-in-time values (balance sheet); '' not NULL so the key works
    period_end     TEXT NOT NULL,
    value          REAL NOT NULL,
    fiscal_year    INTEGER,
    fiscal_period  TEXT,                      -- FY, Q1, Q2, Q3 (of the filing)
    form           TEXT,
    accession_no   TEXT NOT NULL,
    filed_date     TEXT,
    frame          TEXT,                      -- SEC's calendar frame, e.g. CY2026Q3, if assigned
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (ticker, taxonomy, concept, unit, period_start, period_end, accession_no)
);

-- ---------- Milestone 6: daily digest ----------

-- One digest per day. Re-building a day replaces its row and lines.
CREATE TABLE IF NOT EXISTS digests (
    id               INTEGER PRIMARY KEY,
    day              TEXT NOT NULL UNIQUE,
    status           TEXT NOT NULL,           -- ok | partial (a Claude call failed) | blocked (a Python part broke)
    built_at         TEXT NOT NULL,
    covers_since     TEXT,                    -- items first seen after this moment (previous digest); NULL = first digest
    items_used       INTEGER,
    bullets_kept     INTEGER,
    bullets_removed  INTEGER,
    model            TEXT,
    prompt_version   TEXT,
    path             TEXT                     -- data/digests/<day>/digest.md
);

-- Every bullet Claude wrote, kept or removed, with its citations and check result.
CREATE TABLE IF NOT EXISTS digest_lines (
    id         INTEGER PRIMARY KEY,
    digest_id  INTEGER NOT NULL REFERENCES digests(id),
    section    TEXT NOT NULL,                 -- MARKET or a ticker
    seq        INTEGER NOT NULL,
    text       TEXT NOT NULL,
    cites      TEXT NOT NULL,                 -- JSON list of fact IDs
    status     TEXT NOT NULL,                 -- kept | removed
    reason     TEXT,                          -- why it failed (first attempt, or final)
    attempt    INTEGER NOT NULL               -- 1 = first answer, 2 = after the retry
);

-- One dashboard per day (Milestone 6b). Narratives and chart data live in the HTML / JSON files.
CREATE TABLE IF NOT EXISTS dashboards (
    id                 INTEGER PRIMARY KEY,
    day                TEXT NOT NULL UNIQUE,
    status             TEXT NOT NULL,         -- ok | partial (a Claude call failed)
    built_at           TEXT NOT NULL,
    news_rows          INTEGER,
    sentences_kept     INTEGER,
    sentences_removed  INTEGER,
    path               TEXT
);

-- ---------- Milestone 7: delivery and the daily run ----------

-- Every email sent (or attempted): the daily digest and alerts. Stops the digest being sent twice.
CREATE TABLE IF NOT EXISTS deliveries (
    id          INTEGER PRIMARY KEY,
    day         TEXT NOT NULL,
    kind        TEXT NOT NULL,                -- digest | alert
    subject     TEXT,
    recipient   TEXT,
    status      TEXT NOT NULL,                -- sent | failed | dry_run
    error       TEXT,
    size_bytes  INTEGER,
    sent_at     TEXT NOT NULL
);

-- One row per daily run (fetch -> classify -> digest -> dashboard -> send).
CREATE TABLE IF NOT EXISTS daily_runs (
    id           INTEGER PRIMARY KEY,
    day          TEXT NOT NULL,
    started_at   TEXT NOT NULL,
    finished_at  TEXT,
    status       TEXT,                        -- ok | warnings | failed
    steps        TEXT,                        -- JSON: each step's status, seconds, summary
    issues       TEXT,                        -- JSON list of problems (also sent as an alert)
    log_path     TEXT
);

CREATE INDEX IF NOT EXISTS idx_deliveries_day    ON deliveries (day, kind, status);
CREATE INDEX IF NOT EXISTS idx_digest_lines      ON digest_lines (digest_id);
CREATE INDEX IF NOT EXISTS idx_labels_item       ON item_labels (item_id);
CREATE INDEX IF NOT EXISTS idx_quarantine_item   ON llm_quarantine (item_id, resolved);
CREATE INDEX IF NOT EXISTS idx_financials_lookup ON financials (ticker, concept, period_end);

CREATE INDEX IF NOT EXISTS idx_items_published   ON items (published_at);
CREATE INDEX IF NOT EXISTS idx_items_type        ON items (item_type);
CREATE INDEX IF NOT EXISTS idx_items_clean_title ON items (clean_title);
CREATE INDEX IF NOT EXISTS idx_item_tickers      ON item_tickers (ticker);
CREATE INDEX IF NOT EXISTS idx_sections_doc      ON document_sections (document_id, section);
CREATE INDEX IF NOT EXISTS idx_filings_ticker    ON filings (tickers, form, filed_date);
CREATE INDEX IF NOT EXISTS idx_prices_date       ON prices (date);
