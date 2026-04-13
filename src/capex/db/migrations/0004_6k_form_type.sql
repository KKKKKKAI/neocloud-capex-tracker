-- Migration 0004: Add 6-K form_type for quarterly earnings press releases
-- from 20-F filers (BABA, BIDU, GDS, NBIS).
--
-- SQLite doesn't support ALTER CHECK constraints, so we recreate the table.
-- Must disable FK checks to avoid constraint failures during table swap.

PRAGMA foreign_keys = OFF;

-- Step 1: Create new table with expanded constraints
CREATE TABLE source_documents_new (
    id                INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker            TEXT NOT NULL REFERENCES companies(ticker),
    form_type         TEXT NOT NULL CHECK(form_type IN ('10-K', '10-Q', '20-F', '6-K', 'HK-AR', 'HK-IR')),
    filing_date       TEXT NOT NULL,
    period_of_report  TEXT NOT NULL,
    fiscal_year       INTEGER NOT NULL,
    period_token      TEXT NOT NULL CHECK(period_token IN ('AR', 'Q1', 'Q2', 'Q3', 'H1', 'H2')),
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

-- Step 2: Copy all existing data
INSERT INTO source_documents_new SELECT * FROM source_documents;

-- Step 3: Drop old table and rename
DROP TABLE source_documents;
ALTER TABLE source_documents_new RENAME TO source_documents;

PRAGMA foreign_keys = ON;
