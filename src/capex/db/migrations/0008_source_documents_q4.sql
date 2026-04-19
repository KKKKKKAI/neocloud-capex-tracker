-- Migration 0008: allow period_token='Q4' on source_documents.
--
-- Previously the CHECK constraint allowed only AR/Q1/Q2/Q3/H1/H2 on
-- the assumption that SEC filers never produce a Q4 standalone
-- document (Q4 always rolls into the 10-K). That assumption breaks
-- for foreign filers (e.g. BIDU, BABA) who publish a 6-K Q4 earnings
-- press release in February for the prior calendar year. We need a
-- distinct period_token so the Q4 press release doesn't collide with
-- the annual 20-F row at the same period_of_report.

PRAGMA foreign_keys = OFF;

CREATE TABLE source_documents_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL REFERENCES companies(ticker),
    form_type         TEXT NOT NULL CHECK(form_type IN ('10-K', '10-Q', '20-F', '6-K', 'HK-AR', 'HK-IR')),
    filing_date       TEXT NOT NULL,
    period_of_report  TEXT NOT NULL,
    fiscal_year       INTEGER NOT NULL,
    period_token      TEXT NOT NULL CHECK(period_token IN ('AR', 'Q1', 'Q2', 'Q3', 'Q4', 'H1', 'H2')),
    sha256            TEXT NOT NULL UNIQUE,
    raw_path          TEXT NOT NULL,
    canonical_path    TEXT,
    source            TEXT NOT NULL CHECK(source IN ('sec_edgar', 'hkex', 'xbrl_api')),
    source_url        TEXT NOT NULL,
    accession_number  TEXT,
    fetched_at        TEXT NOT NULL,
    fetcher_version   TEXT NOT NULL,
    protocol_version  TEXT NOT NULL,
    UNIQUE(ticker, form_type, period_of_report)
);

INSERT INTO source_documents_new SELECT * FROM source_documents;
DROP TABLE source_documents;
ALTER TABLE source_documents_new RENAME TO source_documents;

CREATE INDEX IF NOT EXISTS idx_source_documents_ticker_period
    ON source_documents(ticker, period_of_report);

PRAGMA foreign_keys = ON;
