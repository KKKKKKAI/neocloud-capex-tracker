"""Human-authored extraction guidance layer.

Reads `data/seeds/human_notes.yaml`, resolves which notes apply to a
given (ticker, metric_key, period) triple, and renders them as a
prompt block that the LLM extractor injects into Agent A's prompt.

A note is a scoped piece of natural-language guidance (e.g., "For BABA
cloud segment, FY23 onwards excludes ByteDance") produced by the
Protocol Elicitation Loop (`capex audit review`). Future extraction
runs read the guidance and apply it without the reviewer having to
re-explain.

Schema v1 fields:
    id                  str, unique  (e.g. "HN-2026-04-20-001")
    scope:
        ticker          str | null   (null → applies to all tickers)
        metric_keys     list[str] | null
        period_range    str | null   ("FY2023+", "FY2021-FY2023", null)
        form_types      list[str] | null
    guidance            str (multiline NL)
    keywords_to_match   list[str]
    cautions            list[str]
    state               "active" | "superseded" | "revoked"
    added_at            ISO datetime
    added_by            str
    source_audit_run_id str
    source_cell_keys    list[str]
    rationale           str (multiline NL)
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

import yaml

REPO_ROOT = Path(__file__).resolve().parents[3]
NOTES_PATH = REPO_ROOT / "data" / "seeds" / "human_notes.yaml"

_cache: dict | None = None


@dataclass
class HumanNoteScope:
    ticker: str | None = None
    metric_keys: list[str] | None = None
    period_range: str | None = None
    form_types: list[str] | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "ticker": self.ticker,
            "metric_keys": self.metric_keys,
            "period_range": self.period_range,
            "form_types": self.form_types,
        }


@dataclass
class HumanNote:
    id: str
    scope: HumanNoteScope
    guidance: str
    keywords_to_match: list[str] = field(default_factory=list)
    cautions: list[str] = field(default_factory=list)
    state: str = "active"
    added_at: str = ""
    added_by: str = "human_review"
    source_audit_run_id: str = ""
    source_cell_keys: list[str] = field(default_factory=list)
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "scope": self.scope.to_dict(),
            "guidance": self.guidance,
            "keywords_to_match": list(self.keywords_to_match),
            "cautions": list(self.cautions),
            "state": self.state,
            "added_at": self.added_at,
            "added_by": self.added_by,
            "source_audit_run_id": self.source_audit_run_id,
            "source_cell_keys": list(self.source_cell_keys),
            "rationale": self.rationale,
        }


def _load_raw(path: Path | None = None) -> dict:
    global _cache
    path = path or NOTES_PATH
    if _cache is None or path != NOTES_PATH:
        if not path.exists():
            return {"schema_version": 1, "notes": []}
        _cache_local = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
        if path == NOTES_PATH:
            _cache = _cache_local
        return _cache_local
    return _cache


def reload() -> None:
    """Drop the cache (useful after edits)."""
    global _cache
    _cache = None


def load_all(path: Path | None = None) -> list[HumanNote]:
    """Return every note in the file as HumanNote dataclasses."""
    raw = _load_raw(path)
    out: list[HumanNote] = []
    for n in raw.get("notes", []) or []:
        scope = n.get("scope") or {}
        out.append(
            HumanNote(
                id=n.get("id", ""),
                scope=HumanNoteScope(
                    ticker=scope.get("ticker"),
                    metric_keys=scope.get("metric_keys"),
                    period_range=scope.get("period_range"),
                    form_types=scope.get("form_types"),
                ),
                guidance=n.get("guidance", ""),
                keywords_to_match=list(n.get("keywords_to_match") or []),
                cautions=list(n.get("cautions") or []),
                state=n.get("state", "active"),
                added_at=n.get("added_at", ""),
                added_by=n.get("added_by", "human_review"),
                source_audit_run_id=n.get("source_audit_run_id", ""),
                source_cell_keys=list(n.get("source_cell_keys") or []),
                rationale=n.get("rationale", ""),
            )
        )
    return out


# ---- Period-range matching -----------------------------------------

_FY_PLUS_RE = re.compile(r"^FY(\d{4})\+$")
_FY_RANGE_RE = re.compile(r"^FY(\d{4})-FY(\d{4})$")
_FY_SINGLE_RE = re.compile(r"^FY(\d{4})$")


def _period_range_matches(period_range: str | None, fiscal_year: int) -> bool:
    """Return True if the given fiscal_year falls inside `period_range`.

    Grammar:
        None | ""          → always matches
        "FY2023"           → exact year
        "FY2023+"          → that year and later
        "FY2021-FY2023"    → inclusive range
    """
    if not period_range:
        return True
    m = _FY_PLUS_RE.match(period_range)
    if m:
        return fiscal_year >= int(m.group(1))
    m = _FY_RANGE_RE.match(period_range)
    if m:
        lo, hi = int(m.group(1)), int(m.group(2))
        return lo <= fiscal_year <= hi
    m = _FY_SINGLE_RE.match(period_range)
    if m:
        return fiscal_year == int(m.group(1))
    # Unknown grammar — be permissive but honest
    return True


def resolve(
    ticker: str | None,
    metric_key: str | None,
    fiscal_year: int | None = None,
    form_type: str | None = None,
    *,
    notes: list[HumanNote] | None = None,
) -> list[HumanNote]:
    """Return the active notes that apply to this (ticker, metric, fy, form)."""
    source = notes if notes is not None else load_all()
    hits: list[HumanNote] = []
    for n in source:
        if n.state != "active":
            continue
        s = n.scope
        if s.ticker and ticker and s.ticker != ticker:
            continue
        if s.metric_keys and metric_key and metric_key not in s.metric_keys:
            continue
        if s.form_types and form_type and form_type not in s.form_types:
            continue
        if fiscal_year is not None:
            if not _period_range_matches(s.period_range, fiscal_year):
                continue
        hits.append(n)
    return hits


# ---- Prompt rendering ----------------------------------------------

def format_for_prompt(notes: list[HumanNote]) -> str:
    """Render a list of notes as a compact markdown block for injection
    into the LLM extractor's system prompt. Returns empty string if
    `notes` is empty."""
    if not notes:
        return ""
    lines: list[str] = [
        "## Company-specific guidance (authored by prior human reviewers)",
        "",
    ]
    for n in notes:
        scope_parts = []
        if n.scope.ticker:
            scope_parts.append(n.scope.ticker)
        if n.scope.metric_keys:
            scope_parts.append("/".join(n.scope.metric_keys))
        if n.scope.period_range:
            scope_parts.append(n.scope.period_range)
        header = " · ".join(scope_parts) if scope_parts else "all"
        lines.append(f"**{header}**  (note {n.id})")
        for g_line in n.guidance.strip().splitlines():
            lines.append(f"- {g_line}")
        if n.keywords_to_match:
            kws = ", ".join(f'"{k}"' for k in n.keywords_to_match)
            lines.append(f"  - Keywords to find: {kws}")
        for c in n.cautions:
            lines.append(f"  - Caution: {c}")
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"


# ---- Writing notes back --------------------------------------------

def next_note_id(notes: list[HumanNote] | None = None) -> str:
    """Generate a fresh note id `HN-YYYY-MM-DD-NNN`."""
    today = datetime.now(timezone.utc).date().isoformat()
    source = notes if notes is not None else load_all()
    prefix = f"HN-{today}-"
    existing_nums = [
        int(n.id[len(prefix):])
        for n in source
        if n.id.startswith(prefix) and n.id[len(prefix):].isdigit()
    ]
    nxt = (max(existing_nums) + 1) if existing_nums else 1
    return f"{prefix}{nxt:03d}"


def append_note(note: HumanNote, path: Path | None = None) -> None:
    """Append `note` to the YAML file atomically, preserving other notes."""
    path = path or NOTES_PATH
    raw = _load_raw(path) or {}
    all_notes = list(raw.get("notes") or [])
    all_notes.append(note.to_dict())
    payload = {
        "schema_version": raw.get("schema_version", 1),
        "notes": all_notes,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(path)
    reload()


def revoke_note(note_id: str, path: Path | None = None) -> bool:
    """Mark a note as revoked. Returns True if found + updated."""
    path = path or NOTES_PATH
    raw = _load_raw(path) or {}
    all_notes = list(raw.get("notes") or [])
    found = False
    for n in all_notes:
        if n.get("id") == note_id:
            n["state"] = "revoked"
            found = True
            break
    if not found:
        return False
    payload = {
        "schema_version": raw.get("schema_version", 1),
        "notes": all_notes,
    }
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(
        yaml.safe_dump(payload, sort_keys=False, allow_unicode=True),
        encoding="utf-8",
    )
    tmp.replace(path)
    reload()
    return True
