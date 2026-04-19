"""Mechanical data-quality checks for the extraction DB.

Nine pure check functions, each takes the relevant slice of DB state
and returns a `CheckResult`. The orchestrator (`audit.fixes` + CLI)
composes these into a per-cell verdict.

All checks are deterministic and side-effect-free — they do NOT mutate
the DB. Fixes live in `audit.fixes` and are only applied when the CLI
is invoked with `--apply`.
"""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
COVERAGE_PATH = REPO_ROOT / "data" / "seeds" / "coverage.yaml"
BOUNDS_PATH = REPO_ROOT / "data" / "seeds" / "audit_bounds.yaml"

# Tolerances
IDENTITY_TOLERANCE = 0.005       # 0.5 %
CROSS_SOURCE_TOLERANCE = 0.005
CONTINUITY_UPPER = 2.5           # jump > 2.5× triggers flag
CONTINUITY_LOWER = 0.4           # jump < 0.4× triggers flag

POSITIVE_METRICS = {"revenue", "property_plant_equipment_net",
                    "depreciation_amortization",
                    "cloud_segment_revenue", "capital_expenditures"}

# XBRL context duration bands per period_type
DURATION_BANDS = {
    "Q1": (75, 105), "Q2": (75, 105), "Q3": (75, 105), "Q4": (75, 105),
    "3M_reported": (75, 105),
    "H1": (160, 200),
    "9M": (250, 290),
    "FY": (350, 370),
}


@dataclass
class CheckResult:
    check_name: str
    passed: bool
    severity: str = "info"      # info / warn / error
    details: dict[str, Any] = field(default_factory=dict)

    def to_json(self) -> str:
        return json.dumps({
            "check": self.check_name,
            "passed": self.passed,
            "severity": self.severity,
            "details": self.details,
        }, sort_keys=True)


# ---------------------------------------------------------------------------
# Helpers / config loaders (memoised)
# ---------------------------------------------------------------------------
_coverage_cache: dict | None = None
_bounds_cache: dict | None = None


def load_coverage() -> dict:
    global _coverage_cache
    if _coverage_cache is None:
        _coverage_cache = yaml.safe_load(COVERAGE_PATH.read_text()) or {}
    return _coverage_cache


def load_bounds() -> dict:
    global _bounds_cache
    if _bounds_cache is None:
        if BOUNDS_PATH.exists():
            _bounds_cache = yaml.safe_load(BOUNDS_PATH.read_text()) or {}
        else:
            _bounds_cache = {}
    return _bounds_cache


# ---------------------------------------------------------------------------
# 1. Gap detection
# ---------------------------------------------------------------------------
def check_gap(
    ticker: str, metric_key: str, fiscal_year: int,
    period_type: str, present: bool, source_doc_present: bool,
) -> CheckResult:
    """Classify a cell as present / gap-fixable / gap-unfixable."""
    if present:
        return CheckResult("gap", True, "info", {"status": "present"})
    if source_doc_present:
        return CheckResult("gap", False, "warn", {
            "status": "gap_fixable",
            "reason": "source_document present, extractor needs to run",
        })
    return CheckResult("gap", False, "info", {
        "status": "gap_unfixable",
        "reason": "no source document available",
    })


# ---------------------------------------------------------------------------
# 2. Arithmetic identity
# ---------------------------------------------------------------------------
def check_identity(group: dict[str, float]) -> CheckResult:
    """Verify Q1+Q2+Q3+Q4 ≈ FY and derived identities for a (ticker, fy, metric)
    group. `group` keys are period_type labels ('Q1', 'H1', 'FY', ...)."""
    issues = []

    def _rel(a: float, b: float) -> float:
        denom = max(abs(a), abs(b), 1.0)
        return abs(a - b) / denom

    def _has(*keys: str) -> bool:
        return all(group.get(k) is not None for k in keys)

    if _has("FY", "Q1", "Q2", "Q3", "Q4"):
        qsum = group["Q1"] + group["Q2"] + group["Q3"] + group["Q4"]
        r = _rel(qsum, group["FY"])
        if r > IDENTITY_TOLERANCE:
            issues.append({
                "identity": "Q1+Q2+Q3+Q4 = FY",
                "rhs": group["FY"], "lhs_sum": qsum,
                "delta_pct": round(r * 100, 3),
            })
    if _has("H1", "Q1", "Q2"):
        r = _rel(group["Q1"] + group["Q2"], group["H1"])
        if r > IDENTITY_TOLERANCE:
            issues.append({
                "identity": "Q1+Q2 = H1",
                "rhs": group["H1"], "lhs_sum": group["Q1"] + group["Q2"],
                "delta_pct": round(r * 100, 3),
            })
    if _has("9M", "Q1", "Q2", "Q3"):
        s = group["Q1"] + group["Q2"] + group["Q3"]
        r = _rel(s, group["9M"])
        if r > IDENTITY_TOLERANCE:
            issues.append({
                "identity": "Q1+Q2+Q3 = 9M",
                "rhs": group["9M"], "lhs_sum": s,
                "delta_pct": round(r * 100, 3),
            })

    if not issues:
        return CheckResult("identity", True, "info")
    return CheckResult("identity", False, "warn", {"violations": issues})


# ---------------------------------------------------------------------------
# 3. Range sanity
# ---------------------------------------------------------------------------
def _normalise_period_key(period_type: str) -> str:
    if period_type in ("Q1", "Q2", "Q3", "Q4", "3M_reported"):
        return "Q1_Q2_Q3_Q4"
    return period_type


def check_range(
    ticker: str, metric_key: str, period_type: str, value_usd: float,
) -> CheckResult:
    bounds = load_bounds() or {}
    key = _normalise_period_key(period_type)
    ranges = (bounds.get("tickers", {}).get(ticker, {}).get(metric_key) or
              bounds.get("defaults", {}).get(metric_key) or {})
    pair = ranges.get(key) or ranges.get(period_type)
    if not pair:
        return CheckResult("range", True, "info",
                           {"status": "no_bounds_defined"})
    lo, hi = pair
    if value_usd < lo or value_usd > hi:
        return CheckResult("range", False, "warn", {
            "value_usd": value_usd, "lo": lo, "hi": hi,
            "outside": "below" if value_usd < lo else "above",
        })
    return CheckResult("range", True, "info")


# ---------------------------------------------------------------------------
# 4. Continuity / jump
# ---------------------------------------------------------------------------
def check_continuity(
    ticker: str, metric_key: str,
    prev_period_label: str, prev_value: float,
    this_period_label: str, this_value: float,
) -> CheckResult:
    if prev_value is None or this_value is None:
        return CheckResult("continuity", True, "info",
                           {"status": "no_prev"})
    if prev_value == 0:
        return CheckResult("continuity", True, "info",
                           {"status": "prev_zero"})
    factor = this_value / prev_value
    if factor > CONTINUITY_UPPER or factor < CONTINUITY_LOWER:
        return CheckResult("continuity", False, "warn", {
            "prev": prev_value, "this": this_value,
            "factor": round(factor, 3),
            "prev_label": prev_period_label,
            "this_label": this_period_label,
        })
    return CheckResult("continuity", True, "info")


# ---------------------------------------------------------------------------
# 5. Cross-source match
# ---------------------------------------------------------------------------
def check_cross_source(
    value_usd: float, evidence_quote: str | None,
) -> CheckResult:
    """Parse numbers out of the evidence quote and confirm the DB value
    appears within tolerance."""
    if not evidence_quote or value_usd is None:
        return CheckResult("cross_source", True, "info",
                           {"status": "no_quote"})
    import re
    nums = [float(m.replace(",", ""))
            for m in re.findall(r"\d[\d,]{2,}(?:\.\d+)?", evidence_quote)
            if float(m.replace(",", "")) > 100]
    if not nums:
        return CheckResult("cross_source", True, "info",
                           {"status": "no_numbers_in_quote"})
    abs_val = abs(value_usd)
    # Accept match if any number in quote is within 0.5 %.
    for n in nums:
        if n == 0:
            continue
        rel = abs(n - abs_val) / max(abs_val, 1.0)
        if rel <= CROSS_SOURCE_TOLERANCE:
            return CheckResult("cross_source", True, "info",
                               {"match": n})
    return CheckResult("cross_source", False, "warn", {
        "value_usd": value_usd,
        "quote_numbers": nums[:8],
    })


# ---------------------------------------------------------------------------
# 6. Sign sanity
# ---------------------------------------------------------------------------
def check_sign(
    ticker: str, metric_key: str, fiscal_year: int,
    value: float, period_type: str,
) -> CheckResult:
    if value is None:
        return CheckResult("sign", True, "info", {"status": "no_value"})
    if metric_key in POSITIVE_METRICS and value < 0:
        # Some 10-Q filers report capex as negative (cash outflow). Accept
        # abs() convention at load time.
        if metric_key == "capital_expenditures":
            return CheckResult("sign", True, "info",
                               {"status": "normalise_abs"})
        return CheckResult("sign", False, "error", {
            "metric_key": metric_key, "value": value,
        })
    if metric_key == "operating_cash_flow":
        # Allow negative for pre-IPO / early-stage tickers
        if value < 0 and ticker not in ("CRWV", "APLD", "IREN", "NBIS"):
            return CheckResult("sign", False, "warn", {
                "metric_key": metric_key, "ticker": ticker,
                "fy": fiscal_year, "value": value,
            })
    return CheckResult("sign", True, "info")


# ---------------------------------------------------------------------------
# 7. Currency & FX
# ---------------------------------------------------------------------------
def check_currency(
    reporting_currency: str, value: float, value_usd: float,
    fx_rate: float | None,
) -> CheckResult:
    if reporting_currency == "USD":
        if value is not None and value_usd is not None and abs(value - value_usd) > 0.5:
            return CheckResult("currency", False, "error", {
                "issue": "USD reporter but value != value_usd",
                "value": value, "value_usd": value_usd,
            })
        return CheckResult("currency", True, "info")

    if value is None or value_usd is None or fx_rate is None:
        return CheckResult("currency", False, "warn", {
            "issue": "missing fx fields",
            "value": value, "value_usd": value_usd, "fx_rate": fx_rate,
        })
    # Derived ratio should equal stored fx_rate within 0.1 %
    if value == 0:
        return CheckResult("currency", True, "info",
                           {"status": "zero_value"})
    derived = value_usd / value
    rel = abs(derived - fx_rate) / max(abs(fx_rate), 1e-6)
    if rel > 0.001:
        return CheckResult("currency", False, "warn", {
            "derived_fx": round(derived, 6),
            "stored_fx": fx_rate, "rel": round(rel, 6),
        })
    return CheckResult("currency", True, "info")


# ---------------------------------------------------------------------------
# 8. Segment definition consistency
# ---------------------------------------------------------------------------
def check_segment_def(
    ticker: str, metric_key: str, locator_section: str,
    quote: str,
) -> CheckResult:
    """Only applies to cloud_segment_revenue. Verifies the locator/quote
    names the canonical segment for this ticker per coverage.yaml."""
    if metric_key != "cloud_segment_revenue":
        return CheckResult("segment_def", True, "info",
                           {"status": "not_applicable"})
    coverage = load_coverage() or {}
    ds = (coverage.get("datasets") or {}).get("cloud_segment_revenue") or {}
    # companies_included is a list of {ticker, treatment, segment_name}
    cfg = next(
        (e for e in (ds.get("companies_included") or [])
         if isinstance(e, dict) and e.get("ticker") == ticker),
        None,
    )
    if not cfg:
        return CheckResult("segment_def", True, "info",
                           {"status": "no_config"})
    segment_candidates = [
        s.lower() for s in [
            cfg.get("segment_name"),
            *(cfg.get("segment_name_aliases") or []),
        ] if s
    ]
    if not segment_candidates:
        return CheckResult("segment_def", True, "info",
                           {"status": "no_segment_names_configured"})
    haystack = f"{locator_section or ''} {quote or ''}".lower()
    if any(s in haystack for s in segment_candidates):
        return CheckResult("segment_def", True, "info")
    return CheckResult("segment_def", False, "warn", {
        "expected_any_of": segment_candidates,
        "haystack_preview": haystack[:200],
    })


# ---------------------------------------------------------------------------
# 9. Period-type semantics
# ---------------------------------------------------------------------------
def check_period_type(
    period_type: str, duration_days: int | None,
) -> CheckResult:
    if not period_type:
        return CheckResult("period_type", True, "info",
                           {"status": "no_period_type"})
    band = DURATION_BANDS.get(period_type)
    if not band or duration_days is None:
        return CheckResult("period_type", True, "info",
                           {"status": "no_duration_available"})
    lo, hi = band
    if duration_days < lo or duration_days > hi:
        return CheckResult("period_type", False, "warn", {
            "period_type": period_type,
            "duration_days": duration_days,
            "expected": band,
        })
    return CheckResult("period_type", True, "info")
