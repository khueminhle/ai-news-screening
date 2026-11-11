# Investment Signal Agent (simulation)

A daily research pipeline for a stock watchlist: it collects SEC filings, headlines from major outlets (RSS), prices, analyst rating counts and macro data, has Claude label and summarise them, checks every number Claude writes against its source, and delivers a cited digest by email plus a private interactive dashboard.

> **Simulation, not investment advice.** The system never says buy, sell or hold in its own voice.

**Status:** Part 1 (milestones 1–7) is running daily on GitHub Actions. Part 2 (RAG over filings + research agent) is next. Full design notes: [`mission-control.md`](mission-control.md).

---

## 1. System architecture

```mermaid
flowchart TB
    WL[("watchlist/watchlist.json<br/>9 tickers")]

    subgraph SRC["External data sources"]
        direction LR
        S1["SEC EDGAR<br/>8-K · 10-Q · 10-K"]
        S2["SEC company facts<br/>XBRL numbers"]
        S3["Finnhub<br/>analyst ratings · earnings dates"]
        S4["RSS news<br/>Reuters · Bloomberg · WSJ · FT · CNBC …<br/>(Google News search + outlet feeds)"]
        S5["Federal Reserve<br/>RSS + full text"]
        S6["FRED<br/>19 macro series"]
        S7["yfinance<br/>daily prices"]
    end

    subgraph CON["connectors/"]
        direction LR
        C1["sec.py"]
        C2["sec_facts.py"]
        C3["finnhub.py"]
        C4["news_rss.py"]
        C5["fed.py"]
        C6["macro_fred.py"]
        C7["prices_yfinance.py"]
    end

    subgraph STORE["storage/ + data/"]
        RAW[("data/raw/&lt;source&gt;/&lt;day&gt;/<br/>original responses + documents")]
        DBPY["db.py · schema.sql<br/>clean · dedupe · update rules"]
        DB[("signals.db<br/>SQLite")]
        PARSE["rag/parse.py<br/>HTML / PDF → sections + tables"]
    end

    subgraph LLM["llm/ — Claude via Claude Code CLI"]
        CLI["claude_client.py<br/>claude --print · no tools<br/>(DEEP DIVE: web search only) · timeout"]
        PR["prompts/*.md"]
        SCH["schemas.py<br/>Pydantic reply schemas"]
    end

    subgraph CHK["checks/"]
        K1["citation_check.py"]
        K2["factcheck.py<br/>numbers in cited facts"]
        K3["advice_guard.py"]
    end

    subgraph PIPE["pipeline/"]
        F["run_fetch.py<br/>+ SEC gap refill"]
        CL["run_classify.py<br/>(M5)"]
        DG["run_digest.py<br/>(M6)"]
        DS["run_dashboard.py<br/>fundamentals.py · deep_dive.py (M6b)"]
        BS["build_site.py"]
        RD{{"run_daily.py<br/>whole day + alerts"}}
    end

    subgraph OUT["Outputs"]
        O1["data/digests/&lt;day&gt;/digest.md"]
        O2["data/dashboards/&lt;day&gt;/dashboard.html"]
        O3["Email<br/>delivery/send_email.py"]
        O4["Cloudflare Pages<br/>behind Cloudflare Access"]
    end

    WL -.-> CON
    S1 --> C1
    S2 --> C2
    S3 --> C3
    S4 --> C4
    S5 --> C5
    S6 --> C6
    S7 --> C7
    CON --> RAW
    RAW --> PARSE --> DBPY
    CON --> DBPY --> DB

    F --> CON
    RD --> F & CL & DG & DS & O3

    DB --> CL
    CL <--> CLI
    DB --> DG
    DG <--> CLI
    DB --> DS
    DS <--> CLI
    PR --> CLI
    CLI --> SCH --> CHK
    CL -- labels --> DB
    CHK -- kept bullets --> O1
    CHK -- kept sentences --> O2
    O1 --> DS
    O1 & O2 --> O3
    O2 --> BS --> O4
```

**How to read it:** connectors fetch everything for the tickers in `watchlist.json`, keep the original responses in `data/raw/`, and store cleaned rows in `signals.db`. Three Claude jobs (classify, digest, dashboard narratives) read only what Python hands them from the database, and every answer goes through the checks before it reaches the digest, the dashboard or the email.

---

## 2. Daily run (GitHub Actions)

```mermaid
sequenceDiagram
    autonumber
    participant CJ as cron-job.org
    participant GH as GitHub Actions<br/>daily_ingest.yml
    participant CA as Actions cache<br/>(data/)
    participant SRC as Data sources
    participant CC as Claude Code CLI
    participant GM as Gmail SMTP
    participant CF as Cloudflare Pages

    CJ->>GH: 18:00 HKT · POST workflow_dispatch
    GH->>GH: check secrets · run offline tests
    CA-->>GH: restore data/ (signals.db, raw files, analyses)
    Note over GH: python -m pipeline.run_daily
    GH->>SRC: run_fetch · all connectors<br/>(+ 2-year SEC refill for new tickers / gaps)
    SRC-->>GH: filings · news · prices · ratings · macro
    GH->>CC: run_classify · 10 items per batch (Haiku)
    CC-->>GH: labels → schema + quote checks → item_labels
    GH->>CC: run_digest · one fact sheet per company (Haiku)
    CC-->>GH: bullets → citation · number · advice checks
    GH->>CC: run_dashboard · stock (Haiku) + macro & financials (Sonnet)
    CC-->>GH: narratives → same checks
    GH->>GM: digest email + live link + dashboard.html
    opt any step had problems
        GH->>GM: alert email listing the issues
    end
    GH->>CF: build_site · wrangler pages deploy
    GH->>CA: save data/ for the next run
    GH->>GH: keep digest / dashboard / log as artifacts (14 days)
```

---

## 3. How every Claude answer is checked

```mermaid
flowchart LR
    A["Python builds the input<br/>from signals.db<br/>(items or a fact sheet,<br/>each fact with an ID)"] --> B["Prompt = rules + input<br/>written to a temp file"]
    B --> C["claude --print &lt; prompt<br/>no tools · no internet"]
    C --> D["JSON reply via stdout"]
    D --> E{"Schema valid?"}
    E -- no --> R
    E -- yes --> F{"Cited IDs exist<br/>in the input?"}
    F -- no --> R
    F -- yes --> G{"Every number in<br/>a cited fact?"}
    G -- no --> R
    G -- yes --> H{"No advice in<br/>Claude's own voice?"}
    H -- no --> R
    H -- yes --> K["Kept<br/>stored / shown"]
    R{"First failure?"} -- yes --> C2["Sent back once<br/>with the reason"] --> D
    R -- "no, failed twice" --> X["Removed and listed<br/>(labels → quarantine)"]
```

---

## 4. Data model (main tables)

```mermaid
erDiagram
    items ||--o{ item_tickers : "tagged with"
    items }o--o| events : "same story"
    items ||--o| filings : "is a"
    items }o--o| documents : "full text"
    filings ||--o{ filing_files : contains
    filing_files }o--o| documents : "downloaded as"
    documents ||--o{ document_sections : "split into"
    items ||--o{ item_labels : "labelled by Claude"
    item_labels ||--o{ item_figures : quotes
    items ||--o{ llm_quarantine : "failed twice"
    digests ||--o{ digest_lines : bullets

    items {
        int id PK
        text source
        text source_id
        text item_type
        text title
        text summary
        text published_at
        text relevance
    }
    filings {
        text accession_no PK
        text form
        text filed_date
        text accepted_at
    }
    documents {
        int id PK
        text doc_type
        text full_text
    }
    document_sections {
        int document_id FK
        int seq
        text section
        text kind
        text content
    }
    item_labels {
        int item_id FK
        text prompt_version
        int relevant
        text event_type
        text importance
    }
    financials {
        text ticker
        text concept
        text period_end
        real value
        text accession_no
    }
    prices {
        text ticker
        text date
        real close
        int is_final
    }
    macro_series {
        text series_id
        text date
        real value
        real revised_from
    }
    analyst_ratings {
        text ticker
        text period
        int buy
        int hold
        int sell
    }
    digests {
        int id PK
        text day
        text status
    }
    digest_lines {
        int digest_id FK
        text section
        text text
        text status
    }
```

Also stored: `earnings_calendar` (+ `earnings_changes`), `series_info`, `sec_coverage` (filing-history coverage per ticker), `llm_runs`, `dashboards`, `deliveries`, `daily_runs`, `fetch_runs`. Every table has a unique key, so re-running a day adds no duplicates.

---

## 5. Repository layout

```
.
├── watchlist/watchlist.json      # the tickers every connector reads
├── config/                       # fred_series.json, news_sources.json (allowed news outlets)
├── connectors/                   # one file per data source (fetch(day) -> items)
├── storage/                      # schema.sql, db.py (save + update rules), inspect_db.py
├── rag/parse.py                  # filings -> text blocks, tables as rows, split by Item
├── llm/                          # claude_client.py, schemas.py, prompts/*.md
├── checks/                       # citation_check, factcheck, advice_guard
├── pipeline/                     # run_fetch, run_classify, run_digest, run_dashboard,
│                                 # fundamentals, build_site, run_daily, dashboard_template.html
├── delivery/send_email.py        # Gmail SMTP: digest + attachment, alerts, no double send
├── tests/                        # offline tests (fake Claude, fake mail server, fixtures)
├── .github/workflows/daily_ingest.yml
└── mission-control.md            # design notes, decisions, milestones
```

---

## 6. Running it locally

```bash
python -m venv venv && venv\Scripts\activate      # Windows
pip install -r requirements.txt
npm install -g @anthropic-ai/claude-code           # Claude Code CLI (sign in once)
copy .env.example .env                             # then fill in the keys

python -m pipeline.run_daily --dry-run             # whole day, email saved to data/outbox/
python -m pytest -q                                # offline tests
```

Single steps: `pipeline.run_fetch`, `pipeline.run_classify`, `pipeline.run_digest`, `pipeline.run_dashboard`, `delivery.send_email --dry-run`, `storage.inspect_db --duplicates`.

**Keys** (names in `.env.example`): `SEC_USER_AGENT`, `FINNHUB_API`, `FRED_API`, `SMTP_USER`, `SMTP_PASSWORD` (Gmail app password), `DIGEST_TO`. On a laptop signed in to Claude Code no Claude key is needed; in the cloud, `CLAUDE_CODE_OAUTH_TOKEN` from `claude setup-token`.

## 7. Running it in the cloud

GitHub Actions runs `daily_ingest.yml` when cron-job.org calls the GitHub API every day at 18:00 HKT. `data/` is kept between runs in the Actions cache. Secrets: the keys above plus `CLAUDE_CODE_OAUTH_TOKEN`, `CLOUDFLARE_API_TOKEN`, `CLOUDFLARE_ACCOUNT_ID`. The dashboard is published to Cloudflare Pages and protected by Cloudflare Access (owner's email only).

## 8. Roadmap

| Part | Milestone | Status |
| --- | --- | --- |
| 1 | M1–M4 Connectors + database | Done |
| 1 | M5 Claude classification + SEC company facts | Done |
| 1 | M6 Digest with fact check · M6b Dashboard | Built |
| 1 | M7 Email, GitHub Actions, live dashboard | Built |
| 2 | M8 RAG over filings (chunk, embed, cited answers) | Next |
| 2 | M9 Research agent (LangGraph, tools, approval) | Planned |
