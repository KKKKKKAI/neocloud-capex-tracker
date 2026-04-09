"""Watcher entry point — placeholder.

Responsibilities (when implemented):
    - Poll SEC EDGAR for new 10-K / 10-Q / 8-K filings for companies in scope.
    - Poll non-US issuer investor-relations pages for new disclosures.
    - Deduplicate against previously-seen filings.
    - Emit NewFilingEvent records (company, filing_type, url, detected_at).

Design notes:
    - No LLM calls in this layer.
    - Runs on GitHub Actions cron, single-writer via concurrency group.
    - Failures must be loud: a missed filing is the primary error mode to guard against.
"""
from __future__ import annotations


def run() -> None:
    """Placeholder entrypoint. Not yet implemented."""
    raise NotImplementedError("Watcher layer is not yet implemented.")


if __name__ == "__main__":
    run()
