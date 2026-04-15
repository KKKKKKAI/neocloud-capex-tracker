-- Migration 0006: Fiscal calendar table for earnings date tracking.
--
-- Stores company-announced earnings release dates from Alpha Vantage.
-- The monitor uses this to know WHEN to poll SEC EDGAR for new filings.

CREATE TABLE fiscal_calendar (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL REFERENCES companies(ticker),
    report_date         TEXT NOT NULL,     -- announced earnings date (YYYY-MM-DD)
    fiscal_date_ending  TEXT NOT NULL,     -- quarter/year-end date (YYYY-MM-DD)
    form_type           TEXT,              -- expected form: 10-Q, 10-K, 20-F, 6-K
    status              TEXT NOT NULL DEFAULT 'upcoming'
        CHECK(status IN ('upcoming', 'detected', 'fetched', 'extracted', 'failed')),
    source              TEXT NOT NULL DEFAULT 'alpha_vantage',
    updated_at          TEXT NOT NULL,
    UNIQUE(ticker, fiscal_date_ending)
);

CREATE INDEX idx_fiscal_calendar_date ON fiscal_calendar(report_date);
CREATE INDEX idx_fiscal_calendar_status ON fiscal_calendar(status);
