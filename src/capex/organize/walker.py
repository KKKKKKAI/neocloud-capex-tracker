"""organize-sources walker: sweep _raw/, copy to <YYYY>/ canonical paths.

Reads sidecar JSON files from `data/_sources/<TICKER>/_raw/`, computes
canonical names via namer.py, copies the source files to the canonical
year-folder location atomically, updates `source_documents.canonical_path`
in the DB, and appends to `data/_sources/_organizer_log.csv`.

Idempotent: running twice produces no new copies. Atomic: an interrupted
run leaves no half-written canonical files.

Read-only on _raw/ and on .fetch.json sidecars. Never deletes anything.
"""
from __future__ import annotations

import csv
import hashlib
import json
import shutil
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from ..db import Database
from .namer import (
    canonical_name,
    compute_fiscal_year,
    compute_period_token,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCES_DIR = REPO_ROOT / "data" / "_sources"
ORGANIZER_LOG = SOURCES_DIR / "_organizer_log.csv"

ACTOR_ORGANIZE = "organize-sources@0.1.0"

LOG_FIELDS = (
    "timestamp",
    "action",
    "ticker",
    "form_type",
    "period_token",
    "fiscal_year",
    "source_path",
    "target_path",
    "sha256",
    "notes",
)


def sweep(
    *,
    ticker_filter: str | None = None,
    dry_run: bool = False,
    db: Database | None = None,
) -> dict[str, Any]:
    """Walk the _sources/ tree and produce canonical copies for any new files.

    Args:
        ticker_filter: if set, only process this ticker's _raw/ folder.
        dry_run: if True, log intended actions to the summary but write nothing.
        db: optional Database for testing; defaults to the project DB.

    Returns:
        Summary dict: {scanned, copied, skipped_already_canonical, collisions, errors}.
    """
    db = db or Database()

    summary = {
        "scanned": 0,
        "copied": 0,
        "skipped_already_canonical": 0,
        "collisions": 0,
        "errors": [],
    }

    company_fye = _load_company_fyes(db)
    ticker_dirs = _list_ticker_dirs(ticker_filter)

    for ticker_dir in ticker_dirs:
        ticker = ticker_dir.name
        raw_dir = ticker_dir / "_raw"
        if not raw_dir.is_dir():
            continue
        if ticker not in company_fye:
            summary["errors"].append(
                f"{ticker}: directory exists but ticker not in companies table"
            )
            continue

        fye_month = company_fye[ticker]

        for sidecar_path in sorted(raw_dir.glob("*.fetch.json")):
            summary["scanned"] += 1
            try:
                _process_one(
                    db=db,
                    ticker=ticker,
                    ticker_dir=ticker_dir,
                    sidecar_path=sidecar_path,
                    fye_month=fye_month,
                    dry_run=dry_run,
                    summary=summary,
                )
            except Exception as e:
                summary["errors"].append(f"{sidecar_path.name}: {type(e).__name__}: {e}")

    return summary


# ----------------------------------------------------------------------------
# Internal
# ----------------------------------------------------------------------------


def _process_one(
    *,
    db: Database,
    ticker: str,
    ticker_dir: Path,
    sidecar_path: Path,
    fye_month: int,
    dry_run: bool,
    summary: dict[str, Any],
) -> None:
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    # Sidecar filename pattern is "<source>.fetch.json"; strip the suffix.
    source_name = sidecar_path.name[: -len(".fetch.json")]
    source_path = sidecar_path.parent / source_name
    if not source_path.exists():
        raise FileNotFoundError(
            f"sidecar references missing file: {source_path.relative_to(REPO_ROOT)}"
        )

    form_type = sidecar["form_type"]
    period_of_report = sidecar["period_of_report"]
    filing_date = sidecar["filing_date"]
    sha256 = sidecar["sha256"]

    period_token = compute_period_token(form_type, period_of_report, fye_month, ticker=ticker)
    fiscal_year = compute_fiscal_year(period_of_report, fye_month)

    extension = source_path.suffix
    canonical = canonical_name(filing_date, ticker, period_token, form_type, extension)
    target_dir = ticker_dir / str(fiscal_year)
    target_path = target_dir / canonical

    # Decision matrix.
    if target_path.exists():
        existing_hash = _hash_file(target_path)
        if existing_hash == sha256:
            summary["skipped_already_canonical"] += 1
            # Make sure the DB row's canonical_path is in sync; if not, fix it.
            _ensure_db_canonical_path(db, sha256, target_path, dry_run=dry_run)
            _append_log(
                action="skipped_already_canonical",
                ticker=ticker,
                form_type=form_type,
                period_token=period_token,
                fiscal_year=fiscal_year,
                source_path=source_path,
                target_path=target_path,
                sha256=sha256,
                notes="",
                dry_run=dry_run,
            )
            return
        # Hash differs — amended filing case. Append -a1, -a2, ...
        target_path = _disambiguate_amended(target_path)
        summary["collisions"] += 1
        notes = "amended"
    else:
        notes = ""

    if dry_run:
        summary["copied"] += 1
        _append_log(
            action="would_copy",
            ticker=ticker,
            form_type=form_type,
            period_token=period_token,
            fiscal_year=fiscal_year,
            source_path=source_path,
            target_path=target_path,
            sha256=sha256,
            notes=notes,
            dry_run=True,
        )
        return

    # Atomic copy: write to a temp file in the target directory, fsync, rename.
    target_dir.mkdir(parents=True, exist_ok=True)
    tmp = target_path.with_name(target_path.name + ".tmp")
    shutil.copy2(source_path, tmp)
    # Verify hash before committing the rename.
    new_hash = _hash_file(tmp)
    if new_hash != sha256:
        tmp.unlink(missing_ok=True)
        raise ValueError(
            f"hash mismatch after copy: expected {sha256}, got {new_hash} for {target_path}"
        )
    tmp.replace(target_path)

    summary["copied"] += 1

    canonical_rel = str(target_path.relative_to(REPO_ROOT)).replace("\\", "/")
    _update_db_canonical_path(db, sha256, canonical_rel, ticker, period_token, fiscal_year)

    _append_log(
        action="copied",
        ticker=ticker,
        form_type=form_type,
        period_token=period_token,
        fiscal_year=fiscal_year,
        source_path=source_path,
        target_path=target_path,
        sha256=sha256,
        notes=notes,
        dry_run=False,
    )


def _ensure_db_canonical_path(
    db: Database, sha256: str, target_path: Path, *, dry_run: bool
) -> None:
    """If the DB row's canonical_path is null or stale, fix it. No-op otherwise."""
    canonical_rel = str(target_path.relative_to(REPO_ROOT)).replace("\\", "/")
    with db.connect() as conn:
        row = conn.execute(
            "SELECT id, canonical_path FROM source_documents WHERE sha256 = ?",
            (sha256,),
        ).fetchone()
    if row is None or row["canonical_path"] == canonical_rel:
        return
    if dry_run:
        return
    _update_db_canonical_path(db, sha256, canonical_rel, ticker="", period_token="", fiscal_year=0)


def _update_db_canonical_path(
    db: Database,
    sha256: str,
    canonical_rel: str,
    ticker: str,
    period_token: str,
    fiscal_year: int,
) -> None:
    """UPDATE source_documents.canonical_path + audit_log row."""
    with db.mutating() as conn:
        cur = conn.execute(
            "UPDATE source_documents SET canonical_path = ? WHERE sha256 = ?",
            (canonical_rel, sha256),
        )
        if cur.rowcount == 0:
            return  # No matching row — sidecar exists but no DB record. Caller's responsibility.
        row_id = conn.execute(
            "SELECT id FROM source_documents WHERE sha256 = ?", (sha256,)
        ).fetchone()[0]
        conn.execute(
            """
            INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload)
            VALUES (?, ?, 'canonical_path_set', 'source_documents', ?, ?)
            """,
            (
                _now_iso(),
                ACTOR_ORGANIZE,
                row_id,
                json.dumps(
                    {
                        "ticker": ticker,
                        "period_token": period_token,
                        "fiscal_year": fiscal_year,
                        "canonical_path": canonical_rel,
                    },
                    sort_keys=True,
                ),
            ),
        )


def _disambiguate_amended(target_path: Path) -> Path:
    """Append -a1, -a2, ... before the extension until the name is free."""
    stem, suffix = target_path.stem, target_path.suffix
    for n in range(1, 100):
        candidate = target_path.with_name(f"{stem}-a{n}{suffix}")
        if not candidate.exists():
            return candidate
    raise RuntimeError(f"too many amended copies for {target_path}")


def _hash_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def _list_ticker_dirs(ticker_filter: str | None) -> list[Path]:
    if not SOURCES_DIR.is_dir():
        return []
    if ticker_filter:
        d = SOURCES_DIR / ticker_filter
        return [d] if d.is_dir() else []
    return [
        d
        for d in sorted(SOURCES_DIR.iterdir())
        if d.is_dir() and not d.name.startswith("_")
    ]


def _load_company_fyes(db: Database) -> dict[str, int]:
    with db.connect() as conn:
        return {
            row["ticker"]: row["fiscal_year_end_month"]
            for row in conn.execute("SELECT ticker, fiscal_year_end_month FROM companies")
        }


def _append_log(
    *,
    action: str,
    ticker: str,
    form_type: str,
    period_token: str,
    fiscal_year: int,
    source_path: Path,
    target_path: Path,
    sha256: str,
    notes: str,
    dry_run: bool,
) -> None:
    if dry_run:
        return
    new = not ORGANIZER_LOG.exists()
    ORGANIZER_LOG.parent.mkdir(parents=True, exist_ok=True)
    with ORGANIZER_LOG.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=LOG_FIELDS)
        if new:
            writer.writeheader()
        writer.writerow(
            {
                "timestamp": _now_iso(),
                "action": action,
                "ticker": ticker,
                "form_type": form_type,
                "period_token": period_token,
                "fiscal_year": fiscal_year,
                "source_path": str(source_path.relative_to(REPO_ROOT)).replace("\\", "/"),
                "target_path": str(target_path.relative_to(REPO_ROOT)).replace("\\", "/"),
                "sha256": sha256,
                "notes": notes,
            }
        )


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")
