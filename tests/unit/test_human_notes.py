"""Tests for the human_notes layer — YAML I/O, scope matching, prompt rendering."""
from __future__ import annotations

import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from capex.audit.human_notes import (
    HumanNote,
    HumanNoteScope,
    _period_range_matches,
    append_note,
    format_for_prompt,
    load_all,
    next_note_id,
    resolve,
    revoke_note,
)

# ---- Period-range matching ----------------------------------------

def test_period_range_none_matches_any():
    assert _period_range_matches(None, 2023) is True
    assert _period_range_matches("", 2020) is True


def test_period_range_plus_from_year():
    assert _period_range_matches("FY2023+", 2022) is False
    assert _period_range_matches("FY2023+", 2023) is True
    assert _period_range_matches("FY2023+", 2025) is True


def test_period_range_explicit_range():
    assert _period_range_matches("FY2021-FY2023", 2020) is False
    assert _period_range_matches("FY2021-FY2023", 2021) is True
    assert _period_range_matches("FY2021-FY2023", 2023) is True
    assert _period_range_matches("FY2021-FY2023", 2024) is False


def test_period_range_single_year():
    assert _period_range_matches("FY2023", 2022) is False
    assert _period_range_matches("FY2023", 2023) is True
    assert _period_range_matches("FY2023", 2024) is False


# ---- Scope matching ------------------------------------------------

def _make_note(**overrides):
    scope_kwargs = {
        "ticker": "BABA",
        "metric_keys": ["cloud_segment_revenue"],
        "period_range": "FY2023+",
        "form_types": None,
    }
    scope_kwargs.update(overrides.pop("scope", {}))
    defaults = dict(
        id="HN-2026-04-20-001",
        scope=HumanNoteScope(**scope_kwargs),
        guidance="FY23 onward excludes ByteDance.",
        keywords_to_match=["Cloud Intelligence Group"],
        cautions=["One-time YoY drop is reclassification"],
        state="active",
    )
    defaults.update(overrides)
    return HumanNote(**defaults)


def test_resolve_matches_on_all_dims():
    n = _make_note()
    hits = resolve("BABA", "cloud_segment_revenue", 2024, notes=[n])
    assert hits == [n]


def test_resolve_excludes_wrong_ticker():
    n = _make_note()
    assert resolve("MSFT", "cloud_segment_revenue", 2024, notes=[n]) == []


def test_resolve_excludes_wrong_metric():
    n = _make_note()
    assert resolve("BABA", "capital_expenditures", 2024, notes=[n]) == []


def test_resolve_excludes_wrong_year():
    n = _make_note()
    assert resolve("BABA", "cloud_segment_revenue", 2021, notes=[n]) == []


def test_resolve_honors_revoked_state():
    n = _make_note(state="revoked")
    assert resolve("BABA", "cloud_segment_revenue", 2024, notes=[n]) == []


def test_resolve_null_ticker_matches_any():
    n = _make_note(scope={"ticker": None})
    assert resolve("MSFT", "cloud_segment_revenue", 2024, notes=[n]) == [n]


def test_resolve_form_type_filter():
    n = _make_note(scope={"form_types": ["10-Q"]})
    assert resolve("BABA", "cloud_segment_revenue", 2024,
                   form_type="10-K", notes=[n]) == []
    assert resolve("BABA", "cloud_segment_revenue", 2024,
                   form_type="10-Q", notes=[n]) == [n]


# ---- Prompt rendering ---------------------------------------------

def test_format_for_prompt_empty_returns_empty_string():
    assert format_for_prompt([]) == ""


def test_format_for_prompt_has_header_and_body():
    out = format_for_prompt([_make_note()])
    assert "Company-specific guidance" in out
    assert "BABA" in out
    assert "cloud_segment_revenue" in out
    assert "FY2023+" in out
    assert "ByteDance" in out
    assert "Cloud Intelligence Group" in out
    assert "Caution:" in out


def test_format_for_prompt_renders_multiple_notes():
    n1 = _make_note(id="HN-A")
    n2 = _make_note(id="HN-B", scope={"ticker": "BIDU"}, guidance="Check iQIYI")
    out = format_for_prompt([n1, n2])
    assert "HN-A" in out
    assert "HN-B" in out
    assert "iQIYI" in out


# ---- File I/O + id generation -------------------------------------

def test_next_note_id_starts_at_001_for_empty_list():
    nid = next_note_id(notes=[])
    today = datetime.now(timezone.utc).date().isoformat()
    assert nid == f"HN-{today}-001"


def test_next_note_id_increments():
    today = datetime.now(timezone.utc).date().isoformat()
    existing = [_make_note(id=f"HN-{today}-001"),
                _make_note(id=f"HN-{today}-004")]
    assert next_note_id(notes=existing) == f"HN-{today}-005"


def test_append_note_round_trip(tmp_path):
    path = tmp_path / "human_notes.yaml"
    path.write_text("schema_version: 1\nnotes: []\n", encoding="utf-8")
    n = _make_note(id="HN-2026-04-20-042")
    append_note(n, path=path)
    loaded = load_all(path)
    assert len(loaded) == 1
    assert loaded[0].id == "HN-2026-04-20-042"
    assert loaded[0].guidance.startswith("FY23")
    # File should still parse as valid YAML
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    assert raw["schema_version"] == 1
    assert len(raw["notes"]) == 1


def test_revoke_note_updates_state(tmp_path):
    path = tmp_path / "human_notes.yaml"
    path.write_text("schema_version: 1\nnotes: []\n", encoding="utf-8")
    n = _make_note(id="HN-X")
    append_note(n, path=path)
    assert revoke_note("HN-X", path=path) is True
    loaded = load_all(path)
    assert loaded[0].state == "revoked"
    # Revoked notes are filtered out by resolve()
    assert resolve("BABA", "cloud_segment_revenue", 2024, notes=loaded) == []


def test_revoke_note_returns_false_when_missing(tmp_path):
    path = tmp_path / "human_notes.yaml"
    path.write_text("schema_version: 1\nnotes: []\n", encoding="utf-8")
    assert revoke_note("HN-DOES-NOT-EXIST", path=path) is False
