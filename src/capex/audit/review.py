"""Capex domain adapter for the Protocol Elicitation Loop.

Reads a data-quality audit JSON sidecar, builds `Anomaly` objects for
the flagged cells, wires up a `Formalizer` + a `ReviewSession` + a
terminal-REPL `ReviewCallbacks`, writes committed artifacts into
`human_notes.yaml`, and logs each interaction to the
`audit_review_feedback` table.

This is the capex-specific piece; all the generic review-loop logic
lives in `src/capex/pel/`.
"""
from __future__ import annotations

import json
import sqlite3
import sys
from collections import defaultdict
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..db import Database
from ..pel import Anomaly, Artifact, Formalizer, ReviewSession
from ..pel.formalizer import FormalizerResult
from ..pel.session import ReviewCallbacks
from . import human_notes

REPO_ROOT = Path(__file__).resolve().parents[3]
REPORT_JSON = REPO_ROOT / "output" / "data_quality_report.json"
PROMPT_PATH = Path(__file__).resolve().parent / "prompts" / "formalize_note.md"


# ---- Load the audit sidecar + build anomalies ----------------------

def _load_report(path: Path | None = None) -> dict[str, Any]:
    path = path or REPORT_JSON
    if not path.exists():
        raise FileNotFoundError(
            f"audit sidecar not found: {path}\n"
            "Run `capex audit` first (the sidecar is emitted alongside "
            "the markdown report)."
        )
    return json.loads(path.read_text(encoding="utf-8"))


def _extraction_quote(conn: sqlite3.Connection, extraction_id: int) -> tuple[str, str]:
    """Return (quote, source_url) for an extraction, or empty strings."""
    row = conn.execute(
        """
        SELECT sd.source_url,
               (SELECT ev.excerpt_text FROM extraction_evidence ev
                WHERE ev.extraction_id = e.id
                  AND ev.excerpt_role = 'primary_value'
                LIMIT 1) AS quote
        FROM extractions e
        JOIN source_documents sd ON e.source_document_id = sd.id
        WHERE e.id = ?
        """,
        (extraction_id,),
    ).fetchone()
    if not row:
        return "", ""
    return (row["quote"] or ""), (row["source_url"] or "")


def _coverage_excerpt(ticker: str, metric_key: str) -> str:
    """Read a relevant slice of coverage.yaml for prompt context."""
    try:
        from ..extract.coverage import get_company_treatment, get_dataset_treatment
        co = get_company_treatment(ticker)
        ds = get_dataset_treatment(ticker, metric_key)
        parts: list[str] = []
        if co:
            parts.append(f"company: {co.full_name} ({co.category})")
            if co.notes:
                parts.append(f"company_notes: {co.notes[:400]}")
        if ds:
            parts.append(f"treatment: {ds.treatment}")
            if ds.segment_names:
                parts.append(f"segment_names: {ds.segment_names}")
            if ds.notes:
                parts.append(f"dataset_notes: {ds.notes[:400]}")
        return "\n".join(parts) or "(no specific treatment configured)"
    except Exception as e:
        return f"(could not load coverage.yaml: {e})"


def _sibling_cells(all_cells: list[dict], ticker: str, metric_key: str) -> list[str]:
    """Collect cell_keys for same (ticker, metric) sorted by period."""
    return [
        c["cell_key"]
        for c in all_cells
        if c["ticker"] == ticker and c["metric_key"] == metric_key
    ]


@dataclass
class ReviewCluster:
    """A group of flagged cells sharing the same (ticker, metric).

    Reviewers rarely want to answer for each cell individually; they
    want to speak once about a coherent problem that touches several
    periods. So the review loop operates at cluster granularity.
    """
    ticker: str
    metric_key: str
    cells: list[dict]       # raw JSON cells (subset with classification=flagged)
    all_sibling_keys: list[str]


def build_clusters(report: dict[str, Any]) -> list[ReviewCluster]:
    cells = report["cells"]
    flagged = [c for c in cells if c["classification"] == "flagged"]
    by_key: dict[tuple[str, str], list[dict]] = defaultdict(list)
    for c in flagged:
        by_key[(c["ticker"], c["metric_key"])].append(c)
    clusters: list[ReviewCluster] = []
    for (ticker, metric_key), group in sorted(by_key.items()):
        group.sort(key=lambda c: (c["fiscal_year"], c["period_type"]))
        clusters.append(
            ReviewCluster(
                ticker=ticker, metric_key=metric_key, cells=group,
                all_sibling_keys=_sibling_cells(cells, ticker, metric_key),
            )
        )
    return clusters


def cluster_to_anomaly(
    cluster: ReviewCluster,
    db: Database,
) -> Anomaly:
    """Convert a cluster into a PEL Anomaly with rich context."""
    cells_ctx: list[dict[str, Any]] = []
    with db.connect() as conn:
        for c in cluster.cells:
            quote, url = (
                _extraction_quote(conn, c["extraction_id"])
                if c.get("extraction_id") else ("", "")
            )
            cells_ctx.append({
                "cell_key": c["cell_key"],
                "fiscal_year": c["fiscal_year"],
                "period_type": c["period_type"],
                "value_usd": c["value_usd"],
                "extracting_model": c["extracting_model"],
                "failed_checks": [
                    {"check": cr["check"], "details": cr["details"]}
                    for cr in c["check_results"] if not cr["passed"]
                ],
                "quote": quote,
                "source_url": url,
            })
    existing_hits = human_notes.resolve(
        cluster.ticker,
        cluster.metric_key,
        fiscal_year=None,
    )
    return Anomaly(
        id=f"{cluster.ticker}:{cluster.metric_key}",
        title=f"{cluster.ticker} — {cluster.metric_key} ({len(cluster.cells)} cells)",
        context={
            "ticker": cluster.ticker,
            "metric_key": cluster.metric_key,
            "cells": cells_ctx,
            "sibling_cells": cluster.all_sibling_keys,
            "current_treatment": _coverage_excerpt(
                cluster.ticker, cluster.metric_key,
            ),
            "existing_notes": [n.to_dict() for n in existing_hits],
        },
    )


# ---- Terminal I/O --------------------------------------------------

def _render_anomaly(a: Anomaly, i: int, total: int) -> None:
    print()
    print(f"[{i}/{total}] {a.title}")
    print("-" * 70)
    cells = a.context.get("cells") or []
    for c in cells:
        val = f"${c['value_usd']:,.1f}M" if c["value_usd"] is not None else "—"
        failed = ", ".join(fc["check"] for fc in c.get("failed_checks") or [])
        print(f"  {c['fiscal_year']}{c['period_type']}: {val:>14s}   "
              f"failed: {failed or '(none)'}")
    first = cells[0] if cells else None
    if first and first.get("quote"):
        print()
        print("  Filing excerpt:")
        for line in str(first["quote"]).strip().splitlines()[:6]:
            print(f"    {line}")
        if first.get("source_url"):
            print(f"    — {first['source_url']}")
    print()
    print(f"  Current treatment: {a.context.get('current_treatment', '')}")
    if a.context.get("existing_notes"):
        print(f"  Existing human notes: {len(a.context['existing_notes'])}")
    print()


def _render_preview(result: FormalizerResult) -> None:
    if result.error:
        print(f"[formalizer error] {result.error}")
        return
    if result.clarifying_questions:
        # already printed by the session loop — skip
        return
    parsed = result.parsed or {}
    note = parsed.get("note") or {}
    scope = note.get("scope") or {}
    print()
    print("Proposed human note:")
    scope_parts = []
    if scope.get("ticker"):
        scope_parts.append(scope["ticker"])
    if scope.get("metric_keys"):
        scope_parts.append("/".join(scope["metric_keys"]))
    if scope.get("period_range"):
        scope_parts.append(scope["period_range"])
    print(f"  scope     {' × '.join(scope_parts) or 'global'}")
    guidance = (note.get("guidance") or "").strip()
    first_line = guidance.splitlines()[0] if guidance else "(empty)"
    print(f"  guidance  {first_line}")
    for extra in guidance.splitlines()[1:4]:
        print(f"            {extra}")
    if note.get("keywords_to_match"):
        kws = " · ".join(f'"{k}"' for k in note["keywords_to_match"][:4])
        print(f"  keywords  {kws}")
    for caution in (note.get("cautions") or [])[:3]:
        print(f"  caution   {caution}")
    linked = parsed.get("linked_cells") or []
    print(f"  affected  {len(linked)} cells ({', '.join(linked[:3])}"
          f"{'…' if len(linked) > 3 else ''})")
    print(f"  confidence {result.confidence}")


def _ask_commit() -> bool:
    ans = input("Apply? [y/N]: ").strip().lower()
    return ans in ("y", "yes")


def _render_summary(outcome) -> None:
    if outcome.action != "committed":
        return
    print(f"  ✔ artifact {outcome.artifact_id} written")
    if outcome.total_after:
        print(f"  ✔ re-audit: {outcome.passed_after}/{outcome.total_after} "
              f"checks now pass")


# ---- Commit + effect + checker -------------------------------------

def _build_artifact(result: FormalizerResult, anomaly: Anomaly) -> Artifact:
    parsed = result.parsed or {}
    note_data = parsed.get("note") or {}
    scope = note_data.get("scope") or {}
    linked = [
        cid for cid in (parsed.get("linked_cells") or [])
        if cid in set(anomaly.context.get("sibling_cells") or [])
    ]
    if not linked:
        # Fall back to the cells actually flagged in this cluster.
        linked = [c["cell_key"] for c in (anomaly.context.get("cells") or [])]
    nid = human_notes.next_note_id()
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    human_note = human_notes.HumanNote(
        id=nid,
        scope=human_notes.HumanNoteScope(
            ticker=scope.get("ticker"),
            metric_keys=scope.get("metric_keys"),
            period_range=scope.get("period_range"),
            form_types=scope.get("form_types"),
        ),
        guidance=(note_data.get("guidance") or "").strip(),
        keywords_to_match=list(note_data.get("keywords_to_match") or []),
        cautions=list(note_data.get("cautions") or []),
        state="active",
        added_at=now,
        added_by="human_review",
        source_audit_run_id="",        # filled by caller (needs run_id)
        source_cell_keys=linked,
    )
    return Artifact(
        id=nid,
        data={"human_note": human_note.to_dict(), "raw": parsed},
        affected_ids=linked,
    )


def _write_artifact(artifact: Artifact, run_id: str) -> None:
    """Append to human_notes.yaml and log to audit_review_feedback."""
    hn = artifact.data["human_note"]
    hn["source_audit_run_id"] = run_id
    note = human_notes.HumanNote(
        id=hn["id"],
        scope=human_notes.HumanNoteScope(**(hn.get("scope") or {})),
        guidance=hn.get("guidance", ""),
        keywords_to_match=hn.get("keywords_to_match") or [],
        cautions=hn.get("cautions") or [],
        state=hn.get("state", "active"),
        added_at=hn.get("added_at", ""),
        added_by=hn.get("added_by", "human_review"),
        source_audit_run_id=run_id,
        source_cell_keys=hn.get("source_cell_keys") or [],
        rationale=hn.get("rationale", ""),
    )
    human_notes.append_note(note)


def _log_interaction(
    db: Database,
    run_id: str,
    anomaly: Anomaly,
    reviewer_input: str,
    result: FormalizerResult,
    artifact: Artifact | None,
) -> None:
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    cell_key = (
        anomaly.context.get("cells")[0]["cell_key"]
        if anomaly.context.get("cells") else anomaly.id
    )
    with db.mutating() as conn:
        conn.execute(
            """
            INSERT OR REPLACE INTO audit_review_feedback
                (audit_run_id, cell_key, human_input, formalized_note_id,
                 formalization_json, reviewer, reviewed_at)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                run_id,
                cell_key,
                reviewer_input,
                artifact.id if artifact else None,
                json.dumps(result.parsed or {"error": result.error},
                           ensure_ascii=False),
                "human_review",
                now,
            ),
        )


def _re_extract(affected_ids: list[str]) -> str:
    """Placeholder effect — re-extract is orchestrated manually today.

    We surface the list of affected cell keys so the reviewer can run
    `capex extract <TICKER> --metric <METRIC> --period <FYQ> --force`
    themselves (the --force flag is wired up separately). This keeps
    the review loop's side-effects fully explicit and reversible.
    """
    if not affected_ids:
        return "no affected cells"
    # Group by ticker + metric for a tidy instruction
    cmds: set[str] = set()
    for cid in affected_ids:
        parts = cid.split(":")
        if len(parts) == 3:
            ticker, metric, _period = parts
            cmds.add(f"capex extract {ticker} --metric {metric} --force")
    return (
        f"note will apply to {len(affected_ids)} cell(s) on the next "
        f"extraction run. To trigger re-extraction now:\n    "
        + "\n    ".join(sorted(cmds))
    )


def _recheck(affected_ids: list[str]) -> tuple[int, int]:
    """No-op re-check for MVP — reported as N/N = informational.

    Full re-audit on a subset requires loading the whole universe, so
    we defer that to the next `capex audit` run. This matches the
    explicit-manual-re-extract contract above.
    """
    return (0, len(affected_ids))


# ---- Public entry point --------------------------------------------

def run_review(
    *,
    cluster_filter: str | None = None,
    limit: int | None = None,
    report_path: Path | None = None,
    formalizer_prompt_path: Path | None = None,
    db: Database | None = None,
) -> int:
    """Top-level review entry. Returns process exit code."""
    report = _load_report(report_path)
    db = db or Database()
    clusters = build_clusters(report)
    if cluster_filter:
        want_t, _, want_m = cluster_filter.partition(":")
        clusters = [
            c for c in clusters
            if c.ticker == want_t and (not want_m or c.metric_key == want_m)
        ]
    if limit is not None:
        clusters = clusters[:limit]

    if not clusters:
        print("No flagged clusters match the filter. Nothing to review.")
        return 0

    prompt_path = formalizer_prompt_path or PROMPT_PATH
    prompt = prompt_path.read_text(encoding="utf-8")

    try:
        from ..adapters.cli_backend import CLIBackend
        backend = CLIBackend.auto()
    except Exception as e:
        print(f"LLM backend unavailable: {e}", file=sys.stderr)
        print("Install `claude`, `gemini`, or `codex` CLI and retry.",
              file=sys.stderr)
        return 2

    formalizer = Formalizer(backend, prompt)
    anomalies = [cluster_to_anomaly(c, db) for c in clusters]

    cb = ReviewCallbacks(
        render_anomaly=_render_anomaly,
        read_input=lambda p: input(p),
        render_preview=_render_preview,
        ask_commit=_ask_commit,
        render_summary=_render_summary,
    )

    run_id = report.get("run_id") or datetime.now(timezone.utc).strftime(
        "review-%Y%m%d-%H%M%S"
    )

    def log(a, user, r, art):
        _log_interaction(db, run_id, a, user, r, art)

    session = ReviewSession(
        anomalies=anomalies,
        formalizer=formalizer,
        write_artifact=_write_artifact,
        effect=_re_extract,
        checker=_recheck,
        build_artifact=_build_artifact,
        callbacks=cb,
        log_interaction=log,
    )
    outcomes = session.run(run_id=run_id)

    committed = [o for o in outcomes if o.action == "committed"]
    skipped = [o for o in outcomes if o.action == "skipped"]
    print()
    print(f"Review session done. Committed {len(committed)}, skipped {len(skipped)}.")
    if committed:
        print("Next step — re-extract affected cells with the new guidance:")
        for o in committed:
            print(f"  {o.anomaly_id}: {o.notes}")
    return 0
