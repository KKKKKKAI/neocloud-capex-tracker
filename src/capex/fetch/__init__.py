"""Source acquisition — fetch authoritative filings from SEC EDGAR or HKEXnews.

Public API:
    fetch_filing(ticker, form_type) → metadata dict
        Dispatch entry point. Looks up the company, picks the per-source
        fetcher, downloads the bytes, writes the file + sidecar, returns
        the metadata that will go into a source_documents row. Does NOT
        write to the DB — see fetch_and_record() for the version that does.

    fetch_and_record(ticker, form_type) → source_documents row id
        Convenience wrapper that runs fetch_filing() and writes the
        source_documents row + audit_log row in one db.mutating() block.

Module layout:
    sidecar.py     — atomic JSON sidecar writer/reader
    sec.py         — SEC EDGAR fetcher (Phase 2a)
    hkex.py        — HKEXnews fetcher (Phase 2b)
    dispatcher.py  — picks the right per-source fetcher

User-Agent for SEC requests:
    SEC EDGAR mandates a real contact email in the User-Agent header.
    Read from CAPEX_FETCHER_UA env var, with a sensible default.
    Override in production deployments to identify yours.
"""
from __future__ import annotations

import os

FETCHER_VERSION = "0.1.0"

DEFAULT_USER_AGENT = "neocloud-capex-tracker f.kai.ye03@gmail.com"


def get_user_agent() -> str:
    """Return the User-Agent string for outbound requests.

    Reads CAPEX_FETCHER_UA from the environment, falling back to the
    project default. Always returns a non-empty string.
    """
    return os.environ.get("CAPEX_FETCHER_UA", DEFAULT_USER_AGENT) or DEFAULT_USER_AGENT
