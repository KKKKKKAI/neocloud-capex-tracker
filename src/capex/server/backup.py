"""Backups: the DB nightly, raw filings weekly, both to the backup bucket.

    capex server backup [--raw] [--no-upload]
    capex server backups                           # what's in the bucket
    capex server restore <key|file> --to PATH [--force]

A DB backup uses SQLite's online backup API (safe while jobs run), is
integrity-checked, then gzipped and uploaded as db/capex-<UTC stamp>.db.gz
beside a gzipped SQL dump. The newest `backup.keep_daily` pairs also stay
on local disk under backups/db/. Raw filings (data/_sources/**) are
copied to raw/ in the bucket; they never change, so only keys that are
missing or differ in size go up. The server can write to the bucket but
not delete from it; the bucket keeps versions and expires old DB backups.

Restore writes a verified copy to --to. Replacing the live DB is a manual
step with the services stopped (docs: server operations runbook).
"""
from __future__ import annotations

import gzip
import os
import shutil
import sqlite3
import tempfile
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .. import paths, settings
from ..db import Database

DB_PREFIX = "db/"
RAW_PREFIX = "raw/"


class BackupError(RuntimeError):
    pass


def _s3(s3: Any) -> Any:
    if s3 is not None:
        return s3
    import boto3
    return boto3.client("s3")


def _bucket(db: Database) -> str:
    return settings.get("backup.bucket", db)


def backup_db(
    *,
    db: Database | None = None,
    upload: bool = True,
    s3: Any = None,
    now: datetime | None = None,
    log: Callable[[str], None] = print,
) -> dict[str, Any]:
    """Snapshot, verify, compress, upload and rotate. Returns a summary."""
    db = db or Database()
    now = now or datetime.now(timezone.utc)
    stamp = now.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    out_dir = paths.backups_dir() / "db"
    out_dir.mkdir(parents=True, exist_ok=True)
    copy = out_dir / f"capex-{stamp}.db"
    db_gz = out_dir / f"capex-{stamp}.db.gz"
    dump_gz = out_dir / f"capex-{stamp}.dump.sql.gz"

    src = sqlite3.connect(db.path, timeout=60)
    dst = sqlite3.connect(copy)
    try:
        with dst:
            src.backup(dst)
        check = dst.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise BackupError(f"integrity_check on the snapshot: {check}")
        with gzip.open(dump_gz, "wt", encoding="utf-8") as out:
            for line in dst.iterdump():
                out.write(line + "\n")
    finally:
        src.close()
        dst.close()
    with open(copy, "rb") as fin, gzip.open(db_gz, "wb") as fout:
        shutil.copyfileobj(fin, fout)
    copy.unlink()
    summary: dict[str, Any] = {"db": db_gz.name, "dump": dump_gz.name,
                               "bytes": db_gz.stat().st_size, "integrity": "ok",
                               "uploaded": []}
    log(f"  snapshot {db_gz.name} ({summary['bytes'] / 2**20:.1f} MiB), integrity ok")

    bucket = _bucket(db)
    if upload and bucket:
        client = _s3(s3)
        for path in (db_gz, dump_gz):
            key = DB_PREFIX + path.name
            client.upload_file(str(path), bucket, key)
            summary["uploaded"].append(key)
            log(f"  uploaded s3://{bucket}/{key}")
    elif upload:
        summary["upload_skipped"] = "backup.bucket is not set: local copy only"
        log("  backup.bucket is not set: local copy only")
    summary["pruned"] = rotate(out_dir, settings.get("backup.keep_daily", db))
    return summary


def rotate(out_dir: Path, keep: int) -> int:
    """Keep the newest `keep` snapshots (a .db.gz + .dump.sql.gz pair each)."""
    stamps = sorted({p.name.split(".", 1)[0] for p in out_dir.glob("capex-*.gz")},
                    reverse=True)
    removed = 0
    for stamp in stamps[keep:]:
        for path in out_dir.glob(f"{stamp}.*"):
            path.unlink()
            removed += 1
    return removed


def sync_raw(*, db: Database | None = None, s3: Any = None,
             log: Callable[[str], None] = print) -> dict[str, Any]:
    """Upload raw filings the bucket doesn't have yet."""
    db = db or Database()
    bucket = _bucket(db)
    if not bucket:
        return {"status": "skipped", "reason": "backup.bucket is not set"}
    root = paths.sources_dir()
    local = {}
    for path in root.rglob("*"):
        rel = path.relative_to(root).as_posix()
        if path.is_file() and not any(part.startswith(".") for part in rel.split("/")):
            local[RAW_PREFIX + rel] = path
    client = _s3(s3)
    remote = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket,
                                                                 Prefix=RAW_PREFIX):
        for obj in page.get("Contents", []):
            remote[obj["Key"]] = obj["Size"]
    todo = sorted(k for k, p in local.items() if remote.get(k) != p.stat().st_size)
    sent = 0
    for key in todo:
        client.upload_file(str(local[key]), bucket, key)
        sent += local[key].stat().st_size
    log(f"  raw filings: {len(local)} local, {len(todo)} uploaded "
        f"({sent / 2**20:.1f} MiB)")
    return {"status": "synced", "files": len(local), "uploaded": len(todo), "bytes": sent}


def list_backups(*, db: Database | None = None, s3: Any = None) -> list[dict[str, Any]]:
    """DB backups in the bucket, newest first."""
    db = db or Database()
    bucket = _bucket(db)
    if not bucket:
        raise BackupError("backup.bucket is not set")
    out = []
    for page in _s3(s3).get_paginator("list_objects_v2").paginate(Bucket=bucket,
                                                                  Prefix=DB_PREFIX):
        for obj in page.get("Contents", []):
            out.append({"key": obj["Key"], "size": obj["Size"],
                        "last_modified": obj["LastModified"]})
    return sorted(out, key=lambda o: o["key"], reverse=True)


def restore_db(
    source: str,
    *,
    to: Path,
    force: bool = False,
    db: Database | None = None,
    s3: Any = None,
    log: Callable[[str], None] = print,
) -> Path:
    """Write the backup `source` (bucket key or local .db.gz) to `to`, verified."""
    from .locks import SCHEDULER, is_locked

    db = db or Database()
    to = Path(to)
    if to.exists() and not force:
        raise BackupError(f"{to} exists; pass force to replace it")
    if to.exists() and to.resolve() == db.path.resolve() and is_locked(SCHEDULER):
        raise BackupError("the scheduler is running on this DB: stop the services first")
    to.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.TemporaryDirectory(dir=to.parent) as tmp:
        gz = Path(source)
        if not gz.exists():
            bucket = _bucket(db)
            if not bucket:
                raise BackupError(f"{source} is not a local file and backup.bucket is not set")
            gz = Path(tmp) / Path(source).name
            _s3(s3).download_file(bucket, source, str(gz))
            log(f"  downloaded s3://{bucket}/{source}")
        restored = Path(tmp) / "restored.db"
        with gzip.open(gz, "rb") as fin, open(restored, "wb") as fout:
            shutil.copyfileobj(fin, fout)
        conn = sqlite3.connect(restored)
        try:
            check = conn.execute("PRAGMA integrity_check").fetchone()[0]
        finally:
            conn.close()
        if check != "ok":
            raise BackupError(f"integrity_check on the restored copy: {check}")
        for suffix in ("-wal", "-shm"):  # a stale WAL would be replayed onto the copy
            stale = to.with_name(to.name + suffix)
            if stale.exists():
                stale.unlink()
        os.replace(restored, to)
    log(f"  restored {source} → {to} (integrity ok)")
    return to
