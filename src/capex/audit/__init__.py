"""Data-quality audit for the extraction DB.

Entry point: `capex audit` CLI (scripts/audit_data_quality.py) which
calls into `audit.checks` for the nine mechanical checks, `audit.fixes`
for remediation, and `audit.report` for the Markdown output. An
optional LLM re-verification pass lives in `audit.llm_reverify`.
"""
