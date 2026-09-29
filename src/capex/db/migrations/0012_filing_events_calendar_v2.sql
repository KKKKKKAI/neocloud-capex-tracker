-- Migration 0012: filing events + fiscal_calendar v2 (watcher state machine).
--
-- fiscal_calendar gains retry bookkeeping and three statuses:
--   partial  some metrics extracted, the rest will be retried
--   stale    the filing never appeared within watcher.stale_after_days
--   skipped  deliberately not processed (e.g. company unwatched)
-- and a link to the filing event that satisfied it.
--
-- filing_events is the unit of work: one row per SEC accession we decided
-- to process (found via a calendar row, a sweep, or by hand). A calendar
-- row's UNIQUE(ticker, fiscal_date_ending) can't hold both a Q4 6-K and the
-- 20-F for the same period end; accessions can.

PRAGMA foreign_keys = OFF;

CREATE TABLE fiscal_calendar_new (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL REFERENCES companies(ticker),
    report_date         TEXT NOT NULL,     -- announced earnings date (YYYY-MM-DD)
    fiscal_date_ending  TEXT NOT NULL,     -- quarter/year-end date (YYYY-MM-DD)
    form_type           TEXT,              -- expected form: 10-Q, 10-K, 20-F, 6-K
    status              TEXT NOT NULL DEFAULT 'upcoming'
        CHECK(status IN ('upcoming', 'detected', 'fetched', 'extracted',
                         'partial', 'failed', 'stale', 'skipped')),
    source              TEXT NOT NULL DEFAULT 'alpha_vantage',
    updated_at          TEXT NOT NULL,
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    last_attempt_at     TEXT,
    next_attempt_at     TEXT,
    filing_event_id     INTEGER,
    UNIQUE(ticker, fiscal_date_ending)
);

INSERT INTO fiscal_calendar_new
    (id, ticker, report_date, fiscal_date_ending, form_type, status, source, updated_at)
SELECT id, ticker, report_date, fiscal_date_ending, form_type, status, source, updated_at
FROM fiscal_calendar;
DROP TABLE fiscal_calendar;
ALTER TABLE fiscal_calendar_new RENAME TO fiscal_calendar;

CREATE INDEX IF NOT EXISTS idx_fiscal_calendar_date ON fiscal_calendar(report_date);
CREATE INDEX IF NOT EXISTS idx_fiscal_calendar_status ON fiscal_calendar(status);

CREATE TABLE IF NOT EXISTS filing_events (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL REFERENCES companies(ticker),
    form_type           TEXT NOT NULL,
    accession_number    TEXT NOT NULL UNIQUE,
    filing_date         TEXT NOT NULL,
    period_of_report    TEXT,             -- EDGAR reportDate (can be blank for 6-K)
    primary_document    TEXT,
    status              TEXT NOT NULL DEFAULT 'discovered'
        CHECK(status IN ('discovered', 'fetched', 'extracted', 'partial',
                         'failed', 'ignored')),
    attempts            INTEGER NOT NULL DEFAULT 0,
    last_error          TEXT,
    next_attempt_at     TEXT,
    source_document_id  INTEGER REFERENCES source_documents(id),
    calendar_id         INTEGER REFERENCES fiscal_calendar(id),
    discovered_by       TEXT NOT NULL CHECK(discovered_by IN ('calendar', 'sweep', 'manual')),
    discovered_at       TEXT NOT NULL,
    updated_at          TEXT NOT NULL,
    summary_json        TEXT
);
CREATE INDEX IF NOT EXISTS idx_filing_events_status ON filing_events(status, next_attempt_at);

CREATE INDEX IF NOT EXISTS idx_source_documents_accession
    ON source_documents(accession_number);

PRAGMA foreign_keys = ON;
