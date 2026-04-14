-- Migration 0005: Add extraction_evidence table for dual-agent verification.
--
-- Stores verbatim text excerpts from filings that prove each extracted
-- value. Multiple excerpts per extraction (primary value, supporting
-- context, derivation inputs, footnotes).

CREATE TABLE extraction_evidence (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id   INTEGER NOT NULL REFERENCES extractions(id),
    excerpt_text    TEXT NOT NULL,
    excerpt_location TEXT,
    excerpt_role    TEXT NOT NULL
        CHECK(excerpt_role IN ('primary_value', 'supporting', 'derivation_input', 'footnote')),
    created_at      TEXT NOT NULL
);

CREATE INDEX idx_evidence_extraction ON extraction_evidence(extraction_id);
