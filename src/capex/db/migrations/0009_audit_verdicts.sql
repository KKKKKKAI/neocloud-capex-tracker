-- Migration 0009: audit_verdicts table for data-quality audit traceability.
--
-- One row per (extraction, audit-run) pair that experienced a mechanical
-- check failure. Records the failing checks, the LLM verdict (if any),
-- and whether a fix was applied. Acts as a trail the report.py renderer
-- reads to produce data_quality_report.md.

CREATE TABLE IF NOT EXISTS audit_verdicts (
    id                       INTEGER PRIMARY KEY AUTOINCREMENT,
    extraction_id            INTEGER NOT NULL REFERENCES extractions(id),
    run_id                   TEXT NOT NULL,
    checked_at               TEXT NOT NULL,
    mechanical_flags_json    TEXT NOT NULL,
    llm_verdict              TEXT,
    llm_response_json        TEXT,
    applied_fix_json         TEXT,
    severity                 TEXT NOT NULL CHECK(severity IN ('info', 'warn', 'error'))
);

CREATE INDEX IF NOT EXISTS idx_audit_verdicts_ext
    ON audit_verdicts(extraction_id);
CREATE INDEX IF NOT EXISTS idx_audit_verdicts_run
    ON audit_verdicts(run_id);
