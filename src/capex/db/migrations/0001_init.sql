-- neocloud-capex-tracker schema v0.1
--
-- Applied by src/capex/db/schema.py::migrate(). Safe to re-run (every
-- statement uses IF NOT EXISTS). The migrator records this version in
-- schema_version after executing.
--
-- See docs/RESTRUCTURING_PLAN.md §5 for rationale, field-by-field notes,
-- and the deliberately-excluded items for v0.1.

PRAGMA foreign_keys = ON;

-- ---------------------------------------------------------------------------
-- 0. Migration tracking
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS schema_version (
    version    INTEGER PRIMARY KEY,
    applied_at TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 1. Companies — mirror of data/_sources/_identity.yaml
--    Refreshed at skill startup via capex.db.sync.sync_companies().
--    YAML is authoritative; this table is a queryable cache.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS companies (
    ticker                TEXT PRIMARY KEY,
    name                  TEXT NOT NULL,
    preferred_source      TEXT NOT NULL CHECK(preferred_source IN ('sec_edgar', 'hkex')),
    edgar_cik             TEXT,
    hkex_stock_code       TEXT,
    fiscal_year_end_month INTEGER NOT NULL CHECK(fiscal_year_end_month BETWEEN 1 AND 12),
    synced_at             TEXT NOT NULL
);

-- ---------------------------------------------------------------------------
-- 2. Source documents — one row per fetched filing
--    Inserted by fetch-company-report, canonical_path populated by
--    organize-sources. sha256 is the immutable identity of the bytes.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS source_documents (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL REFERENCES companies(ticker),
    form_type         TEXT NOT NULL CHECK(form_type IN ('10-K', '10-Q', '20-F', 'HK-AR', 'HK-IR')),
    filing_date       TEXT NOT NULL,   -- ISO date
    period_of_report  TEXT NOT NULL,   -- ISO date
    fiscal_year       INTEGER NOT NULL,
    period_token      TEXT NOT NULL CHECK(period_token IN ('AR', 'Q1', 'Q2', 'Q3', 'H1', 'H2')),
    sha256            TEXT NOT NULL UNIQUE,
    raw_path          TEXT NOT NULL,
    canonical_path    TEXT,            -- null until organize-sources runs
    source            TEXT NOT NULL CHECK(source IN ('sec_edgar', 'hkex')),
    source_url        TEXT NOT NULL,
    accession_number  TEXT,            -- SEC only
    fetched_at        TEXT NOT NULL,
    fetcher_version   TEXT NOT NULL,
    protocol_version  TEXT NOT NULL,
    UNIQUE(ticker, form_type, period_of_report)
);

CREATE INDEX IF NOT EXISTS idx_source_documents_ticker_period
    ON source_documents(ticker, period_of_report);

-- ---------------------------------------------------------------------------
-- 3. Metric definitions — canonical registry for the query skill
--    Seeded from data/seeds/metric_definitions.yaml.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS metric_definitions (
    key          TEXT PRIMARY KEY,
    label        TEXT NOT NULL,
    aliases      TEXT NOT NULL,        -- JSON array
    unit_default TEXT NOT NULL,
    description  TEXT
);

-- ---------------------------------------------------------------------------
-- 4. Extractions — the fact cache
--    One row per (source_document, metric, extracting_model). Queried by
--    query-line-item first; a cache miss triggers read-and-extract, which
--    inserts new rows here.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS extractions (
    id                 INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id INTEGER NOT NULL REFERENCES source_documents(id),
    metric_key         TEXT NOT NULL REFERENCES metric_definitions(key),
    value              REAL,            -- nullable for "not disclosed"
    value_text         TEXT,            -- raw text as it appears in the filing
    unit               TEXT NOT NULL,
    quote              TEXT NOT NULL,
    locator_page       INTEGER,
    locator_section    TEXT,
    extraction_type    TEXT NOT NULL CHECK(extraction_type IN ('direct', 'inferred', 'derived')),
    confidence         REAL,
    extracting_model   TEXT NOT NULL,
    protocol_version   TEXT NOT NULL,
    extracted_at       TEXT NOT NULL,
    UNIQUE(source_document_id, metric_key, extracting_model)
);

CREATE INDEX IF NOT EXISTS idx_extractions_metric
    ON extractions(metric_key);

-- ---------------------------------------------------------------------------
-- 5. Validation results — per-extraction check outcomes
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS validation_results (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id INTEGER NOT NULL REFERENCES extractions(id),
    check_name    TEXT NOT NULL,       -- 'provenance_substring' | 'range_check' | etc
    passed        INTEGER NOT NULL CHECK(passed IN (0, 1)),
    details       TEXT,                 -- JSON
    checked_at    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_validation_results_extraction
    ON validation_results(extraction_id);

-- ---------------------------------------------------------------------------
-- 6. Audit log — append-only record of every DB-mutating action
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS audit_log (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    ts           TEXT NOT NULL,
    actor        TEXT NOT NULL,         -- skill name + version, e.g. "sync-companies@0.0.1"
    action       TEXT NOT NULL,         -- e.g. "companies_upsert"
    target_table TEXT NOT NULL,
    target_id    INTEGER,
    payload      TEXT                   -- JSON of what changed
);

CREATE INDEX IF NOT EXISTS idx_audit_log_ts
    ON audit_log(ts);

-- ---------------------------------------------------------------------------
-- 7. Golden facts — hand-labeled regression baseline
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS golden_facts (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL,
    period            TEXT NOT NULL,
    metric_key        TEXT NOT NULL,
    expected_value    REAL,
    expected_unit     TEXT NOT NULL,
    source_doc_sha256 TEXT NOT NULL,
    note              TEXT,
    UNIQUE(ticker, period, metric_key)
);
