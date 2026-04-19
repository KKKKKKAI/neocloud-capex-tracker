-- Migration 0010: audit_review_feedback table for the Protocol Elicitation Loop.
--
-- One row per human-review interaction: a reviewer sees a flagged cell,
-- gives natural-language guidance, and a formalization sub-agent turns it
-- into a structured human_note. This table is the ledger of those
-- interactions (the human_notes.yaml file is the published artifact).

CREATE TABLE IF NOT EXISTS audit_review_feedback (
    id                    INTEGER PRIMARY KEY AUTOINCREMENT,
    audit_run_id          TEXT NOT NULL,
    cell_key              TEXT NOT NULL,        -- {ticker}:{metric}:{fy}{pt}
    human_input           TEXT NOT NULL,         -- verbatim reviewer text
    formalized_note_id    TEXT,                  -- FK (soft) to human_notes.yaml id
    formalization_json    TEXT,                  -- raw JSON returned by sub-agent
    reviewer              TEXT,                  -- free-text attribution
    reviewed_at           TEXT NOT NULL,
    UNIQUE(audit_run_id, cell_key)
);

CREATE INDEX IF NOT EXISTS idx_arf_run ON audit_review_feedback(audit_run_id);
CREATE INDEX IF NOT EXISTS idx_arf_note ON audit_review_feedback(formalized_note_id);
