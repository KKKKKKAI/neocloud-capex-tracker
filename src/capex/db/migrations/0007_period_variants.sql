-- Migration 0007: period variants + quarterly reporting convention.
--
-- Adds three columns to extractions so a single source_document can
-- contribute multiple period-basis values for the same metric (e.g.,
-- a Q3 10-Q carries both the three-months-ended 3M value and the
-- nine-months-ended 9M YTD value). Also adds company_quarterly_convention
-- to formalize the YTD-vs-standalone decision that's currently
-- hardcoded on form_type.

PRAGMA foreign_keys = OFF;

-- ---------------------------------------------------------------------------
-- 1. Rebuild extractions with period_type, basis_period_months,
--    reporting_convention, and a wider UNIQUE constraint that includes
--    period_type so we can store multiple period variants per filing.
-- ---------------------------------------------------------------------------

CREATE TABLE extractions_new (
    id                   INTEGER PRIMARY KEY AUTOINCREMENT,
    source_document_id   INTEGER NOT NULL REFERENCES source_documents(id),
    metric_key           TEXT NOT NULL REFERENCES metric_definitions(key),
    period_type          TEXT NOT NULL DEFAULT '' CHECK(period_type IN (
        '', 'Q1', 'Q2', 'Q3', 'Q4',
        'H1', 'H2', '9M', 'FY', '3M_reported'
    )),
    basis_period_months  INTEGER,
    reporting_convention TEXT,
    value                REAL,
    value_text           TEXT,
    unit                 TEXT NOT NULL,
    quote                TEXT NOT NULL,
    locator_page         INTEGER,
    locator_section      TEXT,
    extraction_type      TEXT NOT NULL CHECK(extraction_type IN ('direct', 'inferred', 'derived')),
    confidence           REAL,
    extracting_model     TEXT NOT NULL,
    protocol_version     TEXT NOT NULL,
    extracted_at         TEXT NOT NULL,
    value_usd            REAL,
    fx_rate              REAL,
    fx_rate_date         TEXT,
    reporting_currency   TEXT NOT NULL DEFAULT 'USD',
    UNIQUE(source_document_id, metric_key, extracting_model, period_type)
);

INSERT INTO extractions_new (
    id, source_document_id, metric_key,
    period_type, basis_period_months, reporting_convention,
    value, value_text, unit, quote,
    locator_page, locator_section, extraction_type, confidence,
    extracting_model, protocol_version, extracted_at,
    value_usd, fx_rate, fx_rate_date, reporting_currency
)
SELECT
    id, source_document_id, metric_key,
    '', NULL, NULL,
    value, value_text, unit, quote,
    locator_page, locator_section, extraction_type, confidence,
    extracting_model, protocol_version, extracted_at,
    value_usd, fx_rate, fx_rate_date, reporting_currency
FROM extractions;

DROP TABLE extractions;
ALTER TABLE extractions_new RENAME TO extractions;

CREATE INDEX idx_extractions_metric ON extractions(metric_key);
CREATE INDEX idx_extractions_doc_metric_period
    ON extractions(source_document_id, metric_key, period_type);

-- ---------------------------------------------------------------------------
-- 2. company_quarterly_convention — mirror of coverage.yaml quarterly_convention.
--    Kept in a separate table from `companies` because coverage.yaml and
--    _identity.yaml are two distinct YAML sources of truth.
-- ---------------------------------------------------------------------------

CREATE TABLE IF NOT EXISTS company_quarterly_convention (
    ticker             TEXT PRIMARY KEY REFERENCES companies(ticker),
    default_convention TEXT NOT NULL CHECK(default_convention IN (
        'ytd_cumulative',       -- values are year-to-date (3M/6M/9M/12M)
        'standalone_quarterly', -- values are 3-month standalone as reported
        'three_month_column',   -- filing exposes an explicit 3M column; prefer it
        'semi_annual',          -- HKEX IR pattern (H1 only)
        'unknown'               -- flag for manual review
    )),
    per_metric_json    TEXT,    -- JSON object: {"capital_expenditures": "ytd_cumulative"}
    header_signatures_json TEXT,-- JSON: {"expect_any_of": [...], "must_not_match": [...]}
    synced_at          TEXT NOT NULL
);

PRAGMA foreign_keys = ON;
