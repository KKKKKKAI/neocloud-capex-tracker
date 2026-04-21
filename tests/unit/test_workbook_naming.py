"""Tests for the workbook filename convention.

Every exported workbook must land at
    `workbook/[YYYY.MM.DD - HH:MM] financials sourcebook.xlsx`
with ` v2`, ` v3`, ... suffixes when the minute collides.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))


def test_default_workbook_path_minute_stamped(tmp_path):
    from capex.exporters.excel import default_workbook_path
    now = datetime(2026, 4, 21, 9, 16)
    p = default_workbook_path(now=now, workbook_dir=tmp_path)
    assert p == tmp_path / "[2026.04.21 - 09:16] financials sourcebook.xlsx"


def test_default_workbook_path_zero_pads_hour_and_minute(tmp_path):
    from capex.exporters.excel import default_workbook_path
    now = datetime(2026, 1, 5, 3, 7)
    p = default_workbook_path(now=now, workbook_dir=tmp_path)
    # Month, day, hour, minute all two-digit.
    assert p.name == "[2026.01.05 - 03:07] financials sourcebook.xlsx"


def test_default_workbook_path_collision_suffix(tmp_path):
    from capex.exporters.excel import default_workbook_path
    now = datetime(2026, 4, 21, 9, 16)
    first = default_workbook_path(now=now, workbook_dir=tmp_path)
    first.write_bytes(b"")
    second = default_workbook_path(now=now, workbook_dir=tmp_path)
    assert second.name == "[2026.04.21 - 09:16] financials sourcebook v2.xlsx"
    second.write_bytes(b"")
    third = default_workbook_path(now=now, workbook_dir=tmp_path)
    assert third.name == "[2026.04.21 - 09:16] financials sourcebook v3.xlsx"
