"""Cross-check a filing's period headers against coverage.yaml.

Each company declares a `quarterly_convention` in coverage.yaml —
e.g., three_month_column (10-Q with explicit 3M and YTD columns) or
standalone_quarterly (6-K press release with 3M values only). When
extracting a quarterly value, we run this check against the filing
text to catch cases where the declared convention does not match the
filing's observed period headers (e.g., a 6-K that unexpectedly reports
9-month YTD figures).

Usage:
    result = validate_convention(
        ticker="BABA",
        filing_text=section_text,
        conn=conn,
    )
    if not result.ok:
        # raise, or log to audit_log, or force with --force-convention
        ...
"""
from __future__ import annotations

import json
import re
import sqlite3
from dataclasses import dataclass
from typing import Any

THREE_MONTH_RE = re.compile(r"(?i)three[\s\-\u2013\u2014]*months?\s+ended")
SIX_MONTH_RE = re.compile(r"(?i)six[\s\-\u2013\u2014]*months?\s+ended")
NINE_MONTH_RE = re.compile(r"(?i)nine[\s\-\u2013\u2014]*months?\s+ended")
YEAR_ENDED_RE = re.compile(r"(?i)(?:year|twelve[\s\-]?months?|fiscal\s+year)\s+ended")
QUARTER_ENDED_RE = re.compile(r"(?i)quarter(?:ly)?\s+ended")
INTERIM_RE = re.compile(r"(?i)interim\s+(?:report|results|consolidated)")


@dataclass
class ConventionCheck:
    ticker: str
    declared: str | None
    observed_counts: dict[str, int]
    ok: bool
    warnings: list[str]

    def as_payload(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "declared": self.declared,
            "observed_counts": self.observed_counts,
            "warnings": self.warnings,
        }


def _count_headers(text: str) -> dict[str, int]:
    return {
        "three_months_ended": len(THREE_MONTH_RE.findall(text)),
        "six_months_ended": len(SIX_MONTH_RE.findall(text)),
        "nine_months_ended": len(NINE_MONTH_RE.findall(text)),
        "year_ended": len(YEAR_ENDED_RE.findall(text)),
        "quarter_ended": len(QUARTER_ENDED_RE.findall(text)),
        "interim": len(INTERIM_RE.findall(text)),
    }


def _load_convention(conn: sqlite3.Connection, ticker: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT default_convention, per_metric_json, header_signatures_json "
        "FROM company_quarterly_convention WHERE ticker = ?",
        (ticker,),
    ).fetchone()
    if not row:
        return None
    keys = ("default_convention", "per_metric_json", "header_signatures_json")
    return dict(zip(keys, row, strict=True))


def validate_convention(
    *,
    ticker: str,
    filing_text: str,
    conn: sqlite3.Connection,
    metric_key: str | None = None,
) -> ConventionCheck:
    """Compare a filing's period-header patterns with the declared convention.

    If the company is not registered in company_quarterly_convention,
    returns ok=False with a clear warning. Otherwise returns ok=True
    when the observed patterns are consistent with the declared
    convention, and ok=False when they clash (e.g., a
    standalone_quarterly filer unexpectedly showing "nine months ended").
    """
    warnings: list[str] = []
    config = _load_convention(conn, ticker)
    if config is None:
        return ConventionCheck(
            ticker=ticker,
            declared=None,
            observed_counts={},
            ok=False,
            warnings=[
                f"no quarterly_convention declared for {ticker}; "
                f"add one to data/seeds/coverage.yaml and run "
                f"`capex db sync-coverage`"
            ],
        )

    declared = config["default_convention"]
    if metric_key:
        per_metric = json.loads(config.get("per_metric_json") or "{}")
        if metric_key in per_metric:
            declared = per_metric[metric_key]

    counts = _count_headers(filing_text)
    sig = json.loads(config.get("header_signatures_json") or "{}")
    expect_any_of = sig.get("expect_any_of") or []
    must_not_match = sig.get("must_not_match") or []

    for phrase in expect_any_of:
        if phrase.lower() in filing_text.lower():
            break
    else:
        if expect_any_of:
            warnings.append(
                f"none of expect_any_of phrases {expect_any_of!r} found in "
                f"filing text"
            )

    for phrase in must_not_match:
        if phrase.lower() in filing_text.lower():
            warnings.append(
                f"disallowed phrase {phrase!r} appears in filing text "
                f"(conflicts with declared={declared!r})"
            )

    if declared == "standalone_quarterly" and counts["nine_months_ended"] > 0:
        warnings.append(
            "declared standalone_quarterly but found 'nine months ended' "
            "language — filing likely reports YTD, not standalone"
        )
    if declared == "ytd_cumulative" and counts["three_months_ended"] == 0 \
            and counts["nine_months_ended"] == 0:
        warnings.append(
            "declared ytd_cumulative but no 'three/six/nine months ended' "
            "headers found — filing may not be cumulative"
        )
    if declared == "semi_annual" and counts["three_months_ended"] > 0 \
            and counts["six_months_ended"] == 0:
        warnings.append(
            "declared semi_annual but found three-month headers without "
            "six-month headers — filing may be quarterly"
        )

    ok = not warnings
    return ConventionCheck(
        ticker=ticker,
        declared=declared,
        observed_counts=counts,
        ok=ok,
        warnings=warnings,
    )
