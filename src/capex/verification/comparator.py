"""Value comparison with tolerance for dual-agent verification."""
from __future__ import annotations


def compare_values(
    value_a: float | None,
    value_b: float | None,
    *,
    tolerance_exact: float = 0.001,
    tolerance_approx: float = 0.05,
) -> str:
    """Compare two extracted values and return the match type.

    Returns:
        "exact"       — values match within 0.1% (rounding difference)
        "approximate"  — values match within 5% (unit/rounding tolerance)
        "mismatch"     — values differ by more than 5%
        "not_found"    — one or both values are None
    """
    if value_a is None or value_b is None:
        return "not_found"

    if value_a == 0 and value_b == 0:
        return "exact"

    denominator = max(abs(value_a), abs(value_b))
    if denominator == 0:
        return "not_found"

    diff = abs(value_a - value_b) / denominator

    if diff < tolerance_exact:
        return "exact"
    elif diff < tolerance_approx:
        return "approximate"
    else:
        return "mismatch"


def is_verified(match_type: str) -> bool:
    """Return True if the match type counts as verified."""
    return match_type in ("exact", "approximate")
