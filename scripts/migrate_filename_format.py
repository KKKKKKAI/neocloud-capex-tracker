#!/usr/bin/env python3
"""One-time migration: rename [dd.mm.yyyy] filenames to [yyyy.mm.dd].

Renames files on disk, updates source_documents DB rows, and rewrites
sidecar JSON contents. Supports --dry-run for safe preview.

Usage:
    python scripts/migrate_filename_format.py --dry-run   # preview only
    python scripts/migrate_filename_format.py              # execute
"""
from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
SOURCES_DIR = REPO_ROOT / "data" / "_sources"
DB_PATH = REPO_ROOT / "data" / "db" / "capex.db"

# Matches [DD.MM.YYYY] where DD and MM are 2-digit, YYYY is 4-digit.
OLD_DATE_RE = re.compile(r"\[(\d{2})\.(\d{2})\.(\d{4})\]")


def reformat_path(path_str: str) -> str | None:
    """Replace [DD.MM.YYYY] with [YYYY.MM.DD] in a path string.

    Returns the new string if a substitution was made, else None.
    """
    if not OLD_DATE_RE.search(path_str):
        return None
    return OLD_DATE_RE.sub(r"[\3.\2.\1]", path_str)


def build_disk_manifest() -> list[tuple[Path, Path]]:
    """Scan data/_sources/ for files with [DD.MM.YYYY] names.

    Returns a list of (old_path, new_path) pairs covering:
    - data files (.htm, .pdf) in _raw/ and year folders
    - sidecar .fetch.json files
    """
    manifest: list[tuple[Path, Path]] = []
    for path in sorted(SOURCES_DIR.rglob("*")):
        if not path.is_file():
            continue
        new_name = reformat_path(path.name)
        if new_name is None:
            continue
        new_path = path.parent / new_name
        manifest.append((path, new_path))
    return manifest


def build_db_manifest(
    conn: sqlite3.Connection,
) -> list[tuple[int, str, str | None, str, str | None]]:
    """Build list of DB rows that need updating.

    Returns: [(id, old_raw_path, old_canonical_path, new_raw_path, new_canonical_path), ...]
    """
    rows = conn.execute(
        "SELECT id, raw_path, canonical_path FROM source_documents"
    ).fetchall()
    manifest = []
    for row in rows:
        doc_id = row[0]
        old_raw = row[1]
        old_canon = row[2]
        new_raw = reformat_path(old_raw)
        new_canon = reformat_path(old_canon) if old_canon else None
        if new_raw or new_canon:
            manifest.append((
                doc_id,
                old_raw,
                old_canon,
                new_raw or old_raw,
                new_canon or old_canon,
            ))
    return manifest


def update_sidecar_content(sidecar_path: Path, dry_run: bool) -> bool:
    """Rewrite the raw_path field inside a sidecar JSON.

    Returns True if the content was changed (or would be in dry-run).
    """
    try:
        data = json.loads(sidecar_path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, FileNotFoundError):
        return False

    old_raw = data.get("raw_path", "")
    new_raw = reformat_path(old_raw)
    if not new_raw:
        return False

    if dry_run:
        return True

    data["raw_path"] = new_raw
    tmp = sidecar_path.with_name(sidecar_path.name + ".tmp")
    payload = json.dumps(data, indent=2, sort_keys=True, ensure_ascii=False) + "\n"
    tmp.write_text(payload, encoding="utf-8")
    tmp.replace(sidecar_path)
    return True


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the rename manifest without making changes.",
    )
    args = parser.parse_args()
    dry_run: bool = args.dry_run

    if dry_run:
        print("=== DRY RUN — no changes will be made ===\n")

    # ---- 1. Build manifests ------------------------------------------------
    print("Scanning files...")
    disk_manifest = build_disk_manifest()

    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    db_manifest = build_db_manifest(conn)

    # Separate data files from sidecars for ordering
    data_renames = [(o, n) for o, n in disk_manifest if not o.name.endswith(".fetch.json")]
    sidecar_renames = [(o, n) for o, n in disk_manifest if o.name.endswith(".fetch.json")]

    print(f"  Data files to rename:    {len(data_renames)}")
    print(f"  Sidecar files to rename: {len(sidecar_renames)}")
    print(f"  DB rows to update:       {len(db_manifest)}")
    print()

    # ---- 2. Pre-flight checks ----------------------------------------------
    errors = []
    targets_seen: set[Path] = set()
    for old, new in disk_manifest:
        if not old.exists():
            errors.append(f"  Source missing: {old}")
        if new.exists() and new != old:
            errors.append(f"  Target exists:  {new}")
        if new in targets_seen:
            errors.append(f"  Duplicate target: {new}")
        targets_seen.add(new)

    if errors:
        print("PRE-FLIGHT ERRORS:")
        for e in errors:
            print(e)
        print("\nAborting.")
        sys.exit(1)

    print("Pre-flight checks passed.\n")

    if dry_run:
        print("--- Disk renames (first 20) ---")
        for old, new in disk_manifest[:20]:
            rel_old = old.relative_to(REPO_ROOT)
            rel_new = new.relative_to(REPO_ROOT)
            print(f"  {rel_old}")
            print(f"    -> {rel_new}")
        if len(disk_manifest) > 20:
            print(f"  ... and {len(disk_manifest) - 20} more")
        print()

        print("--- DB updates (first 10) ---")
        for doc_id, old_raw, old_canon, new_raw, new_canon in db_manifest[:10]:
            print(f"  id={doc_id}: raw_path {old_raw}")
            print(f"           -> {new_raw}")
            if old_canon and new_canon and old_canon != new_canon:
                print(f"         canon {old_canon}")
                print(f"           -> {new_canon}")
        if len(db_manifest) > 10:
            print(f"  ... and {len(db_manifest) - 10} more")
        print()

        # Count sidecars that would have content updated
        sidecar_content_count = 0
        for old, _ in sidecar_renames:
            if update_sidecar_content(old, dry_run=True):
                sidecar_content_count += 1
        print(f"Sidecar JSON content updates: {sidecar_content_count}")
        print("\nDry run complete. Run without --dry-run to execute.")
        conn.close()
        return

    # ---- 3. DB update (single transaction) ---------------------------------
    print("Updating database...")
    try:
        for doc_id, _, _, new_raw, new_canon in db_manifest:
            conn.execute(
                "UPDATE source_documents SET raw_path = ?, canonical_path = ? WHERE id = ?",
                (new_raw, new_canon, doc_id),
            )
        # Audit log entry
        conn.execute(
            "INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (
                datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "filename-format-migration@1.0",
                "filename_format_migrated",
                "source_documents",
                None,
                json.dumps({
                    "old_format": "[dd.mm.yyyy]",
                    "new_format": "[yyyy.mm.dd]",
                    "db_rows_updated": len(db_manifest),
                }),
            ),
        )
        conn.commit()
        print(f"  {len(db_manifest)} rows updated + audit log entry.\n")
    except Exception as exc:
        conn.rollback()
        print(f"  DB update FAILED, rolled back: {exc}")
        conn.close()
        sys.exit(1)
    conn.close()

    # Regenerate dump.sql using the project's dump module
    print("Regenerating dump.sql...")
    sys.path.insert(0, str(REPO_ROOT / "src"))
    from capex.db.dump import dump_to_sql
    dump_to_sql(DB_PATH, REPO_ROOT / "data" / "db" / "dump.sql")
    print("  Done.\n")

    # ---- 4. Update sidecar JSON content ------------------------------------
    print("Updating sidecar JSON content...")
    sidecar_content_count = 0
    for old_path, _ in sidecar_renames:
        if update_sidecar_content(old_path, dry_run=False):
            sidecar_content_count += 1
    print(f"  {sidecar_content_count} sidecars updated.\n")

    # ---- 5. Rename data files on disk --------------------------------------
    print("Renaming data files...")
    for old, new in data_renames:
        old.rename(new)
    print(f"  {len(data_renames)} data files renamed.\n")

    # ---- 6. Rename sidecar files on disk -----------------------------------
    print("Renaming sidecar files...")
    for old, new in sidecar_renames:
        old.rename(new)
    print(f"  {len(sidecar_renames)} sidecar files renamed.\n")

    # ---- 7. Post-flight verification ---------------------------------------
    print("Post-flight verification...")
    conn = sqlite3.connect(DB_PATH)
    conn.execute("PRAGMA foreign_keys = ON")
    missing = []
    rows = conn.execute("SELECT id, raw_path, canonical_path FROM source_documents").fetchall()
    for row in rows:
        raw = REPO_ROOT / row[1]
        if not raw.exists():
            missing.append(f"  raw_path missing: id={row[0]} {row[1]}")
        if row[2]:
            canon = REPO_ROOT / row[2]
            if not canon.exists():
                missing.append(f"  canonical_path missing: id={row[0]} {row[2]}")
    conn.close()

    if missing:
        print("WARNINGS — some DB paths don't resolve:")
        for m in missing:
            print(m)
    else:
        print("  All DB paths resolve to existing files.")

    print("\nMigration complete.")


if __name__ == "__main__":
    main()
