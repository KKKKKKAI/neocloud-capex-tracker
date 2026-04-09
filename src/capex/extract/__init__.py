"""Per-PDF extraction worker.

One invocation = one PDF = one isolated subagent context. Writes
extractions rows to the DB. See skills/read-and-extract/SKILL.md.

Implementation lands in Phase 3.
"""
