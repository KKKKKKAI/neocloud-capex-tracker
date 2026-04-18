"""Period-over-period growth helpers for the interactive chart.

Pure-Python reference implementation. The browser-side JS inside
`interactive_chart._HTML_TEMPLATE` mirrors this logic line-for-line so
both sides stay in sync — if you change the math here, update the JS.

Two modes are supported:

- ``yoy`` — year-over-year. For quarterly labels like ``"2025Q3"`` the
  comparator is the same-quarter-one-year-earlier (``"2024Q3"``). For
  annual labels like ``"FY2024"`` the comparator is the immediately
  previous element (``"FY2023"``) in the series.
- ``qoq`` — quarter-over-quarter / sequential. The comparator is always
  the immediately previous element.

Missing comparators (index out of range, label not found, or a zero /
null value on either side) yield ``None`` so the chart shows a gap
rather than a divide-by-zero spike.
"""
from __future__ import annotations

from collections.abc import Sequence


def prior_index_for(
    i: int,
    x_labels: Sequence[str],
    mode: str,
    period: str,
) -> int:
    """Return the index of the comparator for position ``i``, or -1.

    Args:
        i: current index within ``x_labels``.
        x_labels: label list (``"2019Q1"`` / ``"FY2019"`` style).
        mode: ``"yoy"`` or ``"qoq"``.
        period: ``"annual"`` or ``"quarterly"``.
    """
    if mode == "qoq":
        return i - 1
    if mode != "yoy":
        raise ValueError(f"unknown mode: {mode!r}")
    if period == "annual":
        return i - 1
    if period != "quarterly":
        raise ValueError(f"unknown period: {period!r}")
    # Quarterly YoY: same-quarter-1-year-ago by label parse.
    cur = x_labels[i]
    if len(cur) < 6 or cur[4] != "Q":
        return -1
    try:
        year = int(cur[:4])
    except ValueError:
        return -1
    target = f"{year - 1}{cur[4:]}"
    try:
        return x_labels.index(target)
    except ValueError:
        return -1


def compute_growth(
    series: Sequence[float | None],
    x_labels: Sequence[str],
    mode: str,
    period: str,
) -> list[float | None]:
    """Return a list aligned with ``x_labels`` containing growth % values.

    ``None`` is emitted for positions where either the current or the
    comparator value is missing, zero, or non-positive.
    """
    if len(series) != len(x_labels):
        raise ValueError(
            f"series length {len(series)} != x_labels length {len(x_labels)}"
        )
    out: list[float | None] = []
    for i in range(len(x_labels)):
        pi = prior_index_for(i, x_labels, mode, period)
        if pi < 0 or pi >= len(series):
            out.append(None)
            continue
        cur = series[i]
        prev = series[pi]
        if cur is None or prev is None or prev == 0 or cur == 0:
            out.append(None)
            continue
        out.append((float(cur) / float(prev) - 1.0) * 100.0)
    return out
