"""Tests for the workbook filename convention.

Every exported workbook must land at
    `workbook/[YYYY.MM.DD - HHhMM] financials sourcebook.xlsx`
with ` v2`, ` v3`, ... suffixes when the minute collides. The name must
stay legal on Windows (no ':'), because the repo is checked out on NTFS.
"""
from __future__ import annotations

import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

WINDOWS_ILLEGAL = set('<>:"/\\|?*')


def test_default_workbook_path_minute_stamped(tmp_path):
    from capex.exporters.excel import default_workbook_path
    now = datetime(2026, 4, 21, 9, 16)
    p = default_workbook_path(now=now, workbook_dir=tmp_path)
    assert p == tmp_path / "[2026.04.21 - 09h16] financials sourcebook.xlsx"


def test_default_workbook_path_zero_pads_hour_and_minute(tmp_path):
    from capex.exporters.excel import default_workbook_path
    now = datetime(2026, 1, 5, 3, 7)
    p = default_workbook_path(now=now, workbook_dir=tmp_path)
    # Month, day, hour, minute all two-digit.
    assert p.name == "[2026.01.05 - 03h07] financials sourcebook.xlsx"


def test_default_workbook_path_collision_suffix(tmp_path):
    from capex.exporters.excel import default_workbook_path
    now = datetime(2026, 4, 21, 9, 16)
    first = default_workbook_path(now=now, workbook_dir=tmp_path)
    first.write_bytes(b"")
    second = default_workbook_path(now=now, workbook_dir=tmp_path)
    assert second.name == "[2026.04.21 - 09h16] financials sourcebook v2.xlsx"
    second.write_bytes(b"")
    third = default_workbook_path(now=now, workbook_dir=tmp_path)
    assert third.name == "[2026.04.21 - 09h16] financials sourcebook v3.xlsx"


def test_workbook_name_is_windows_safe(tmp_path):
    from capex.exporters.excel import default_workbook_path
    name = default_workbook_path(now=datetime(2026, 12, 31, 23, 59), workbook_dir=tmp_path).name
    assert not WINDOWS_ILLEGAL & set(name)
    assert not name.endswith((" ", "."))


def test_default_workbook_path_uses_capex_tz(tmp_path, monkeypatch):
    from capex.exporters.excel import default_workbook_path, parse_workbook_name
    monkeypatch.setenv("CAPEX_TZ", "UTC")
    utc_name = default_workbook_path(workbook_dir=tmp_path).name
    monkeypatch.setenv("CAPEX_TZ", "Asia/Tokyo")  # UTC+9, no DST
    tokyo_name = default_workbook_path(workbook_dir=tmp_path).name
    utc_ts, _ = parse_workbook_name(utc_name)
    tokyo_ts, _ = parse_workbook_name(tokyo_name)
    # Same instant, different wall clocks (allow for a minute tick).
    delta_hours = (tokyo_ts - utc_ts).total_seconds() / 3600
    assert 8.9 < delta_hours < 9.1


def test_parse_workbook_name_current_and_legacy_forms():
    from capex.exporters.excel import parse_workbook_name
    ts = datetime(2026, 8, 14, 11, 3)
    assert parse_workbook_name("[2026.08.14 - 11h03] financials sourcebook.xlsx") == (ts, 1)
    assert parse_workbook_name("[2026.08.14 - 11:03] financials sourcebook v2.xlsx") == (ts, 2)
    # WSL stores ':' as U+F03A on NTFS; Windows tools see that character.
    assert parse_workbook_name("[2026.08.14 - 1103] financials sourcebook.xlsx") == (ts, 1)
    assert parse_workbook_name("capex_tracker_v3.xlsx") is None
    assert parse_workbook_name("[2026.08.14 - 11h03] financials sourcebook.xlsm") is None


def test_format_round_trips_through_parse():
    from capex.exporters.excel import format_workbook_name, parse_workbook_name
    ts = datetime(2026, 5, 4, 22, 41)
    for version in (1, 2, 10):
        assert parse_workbook_name(format_workbook_name(ts, version)) == (ts, version)


def test_latest_workbook_ranks_by_timestamp_then_version(tmp_path):
    from capex.exporters.excel import latest_workbook
    names = [
        "[2026.08.14 - 10h54] financials sourcebook.xlsx",
        "[2026.08.14 - 11h03] financials sourcebook.xlsx",
        "[2026.08.14 - 11h03] financials sourcebook v2.xlsx",
        "[2026.08.14 - 11h03] financials sourcebook v10.xlsx",
        "[2026.07.03 - 11h42] financials sourcebook v9.xlsx",
        "notes.xlsx",
    ]
    for n in names:
        (tmp_path / n).write_bytes(b"")
    # Lexicographic order would pick ' v2' (space < '.') or 'v9'; the
    # parsed (timestamp, version) key picks v10 of the newest minute.
    assert latest_workbook(tmp_path).name == names[3]


def test_latest_workbook_empty_dir(tmp_path):
    from capex.exporters.excel import latest_workbook
    assert latest_workbook(tmp_path) is None
