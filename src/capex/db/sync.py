"""YAML → DB sync functions.

Two tables are hand-maintained via YAML and mirrored into the DB for
queryability: companies (from data/_sources/_identity.yaml) and
metric_definitions (from data/seeds/metric_definitions.yaml).

Design:
    * YAML is the source of truth. The DB is a cache that gets wiped &
      refilled by these functions (upsert-style, not drop-and-recreate —
      see the FK-safety discussion below).
    * Called at skill startup to ensure the DB reflects the latest YAML.
      Typical cost: a few ms.
    * Every run writes an audit_log entry so the history of sync
      operations is visible in dump.sql.

FK safety:
    * DELETE FROM companies would cascade-fail if source_documents rows
      reference a company. We never want to silently lose data, so:
      - Upsert every row present in the YAML (INSERT ... ON CONFLICT).
      - For rows that disappeared from the YAML, only delete if no
        downstream table references them. Otherwise raise — the user
        must resolve the inconsistency by hand.
    * metric_definitions has the same property relative to extractions.
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import yaml

from .schema import REPO_ROOT, Database

IDENTITY_PATH = REPO_ROOT / "data" / "_sources" / "_identity.yaml"
METRICS_SEED_PATH = REPO_ROOT / "data" / "seeds" / "metric_definitions.yaml"

ACTOR_COMPANIES = "sync-companies@0.0.1"
ACTOR_METRICS = "sync-metric-definitions@0.0.1"


def sync_companies(
    db: Database | None = None,
    yaml_path: Path | None = None,
) -> int:
    """Refresh the companies table from _identity.yaml.

    Returns the number of companies in the YAML (equal to the row count
    in the companies table after the sync).

    Raises ValueError if a company was removed from the YAML but is
    still referenced by source_documents rows — the user must resolve
    that by hand.
    """
    db = db or Database()
    yaml_path = yaml_path or IDENTITY_PATH

    data = yaml.safe_load(yaml_path.read_text())
    companies = (data or {}).get("companies") or {}
    now = _now_iso()

    with db.mutating() as conn:
        yaml_tickers = set(companies.keys())
        existing = {row[0] for row in conn.execute("SELECT ticker FROM companies")}

        inserted = 0
        updated = 0
        for ticker, entry in companies.items():
            is_new = ticker not in existing
            conn.execute(
                """
                INSERT INTO companies (
                    ticker, name, preferred_source, edgar_cik,
                    hkex_stock_code, fiscal_year_end_month, reporting_currency, synced_at
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(ticker) DO UPDATE SET
                    name = excluded.name,
                    preferred_source = excluded.preferred_source,
                    edgar_cik = excluded.edgar_cik,
                    hkex_stock_code = excluded.hkex_stock_code,
                    fiscal_year_end_month = excluded.fiscal_year_end_month,
                    reporting_currency = excluded.reporting_currency,
                    synced_at = excluded.synced_at
                """,
                (
                    ticker,
                    entry["name"],
                    entry["preferred_source"],
                    entry.get("edgar_cik"),
                    entry.get("hkex_stock_code"),
                    entry["fiscal_year_end_month"],
                    entry.get("reporting_currency", "USD"),
                    now,
                ),
            )
            if is_new:
                inserted += 1
            else:
                updated += 1

        # Handle removals: only safe if nothing references the ticker.
        deleted = 0
        orphans = existing - yaml_tickers
        for ticker in sorted(orphans):
            ref_count = conn.execute(
                "SELECT COUNT(*) FROM source_documents WHERE ticker = ?", (ticker,)
            ).fetchone()[0]
            if ref_count > 0:
                raise ValueError(
                    f"Cannot remove {ticker} from companies: "
                    f"{ref_count} source_documents still reference it. "
                    f"Resolve by hand before re-running sync."
                )
            conn.execute("DELETE FROM companies WHERE ticker = ?", (ticker,))
            deleted += 1

        _audit(
            conn,
            actor=ACTOR_COMPANIES,
            action="companies_sync",
            target_table="companies",
            payload={
                "yaml_path": str(yaml_path.relative_to(REPO_ROOT)),
                "inserted": inserted,
                "updated": updated,
                "deleted": deleted,
                "total": len(companies),
            },
        )

    return len(companies)


def sync_metric_definitions(
    db: Database | None = None,
    yaml_path: Path | None = None,
) -> int:
    """Refresh the metric_definitions table from the YAML seed.

    Returns the number of metric definitions in the YAML.

    Raises ValueError if a metric was removed from the YAML but is
    still referenced by extractions rows.
    """
    db = db or Database()
    yaml_path = yaml_path or METRICS_SEED_PATH

    data = yaml.safe_load(yaml_path.read_text())
    metrics = (data or {}).get("metrics") or {}

    with db.mutating() as conn:
        yaml_keys = set(metrics.keys())
        existing = {row[0] for row in conn.execute("SELECT key FROM metric_definitions")}

        inserted = 0
        updated = 0
        for key, entry in metrics.items():
            is_new = key not in existing
            aliases_json = json.dumps(entry.get("aliases") or [], ensure_ascii=False)
            conn.execute(
                """
                INSERT INTO metric_definitions (
                    key, label, aliases, unit_default, description
                ) VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(key) DO UPDATE SET
                    label = excluded.label,
                    aliases = excluded.aliases,
                    unit_default = excluded.unit_default,
                    description = excluded.description
                """,
                (
                    key,
                    entry["label"],
                    aliases_json,
                    entry["unit_default"],
                    (entry.get("description") or "").strip(),
                ),
            )
            if is_new:
                inserted += 1
            else:
                updated += 1

        deleted = 0
        orphans = existing - yaml_keys
        for key in sorted(orphans):
            ref_count = conn.execute(
                "SELECT COUNT(*) FROM extractions WHERE metric_key = ?", (key,)
            ).fetchone()[0]
            if ref_count > 0:
                raise ValueError(
                    f"Cannot remove metric '{key}' from metric_definitions: "
                    f"{ref_count} extractions still reference it. "
                    f"Resolve by hand before re-running sync."
                )
            conn.execute("DELETE FROM metric_definitions WHERE key = ?", (key,))
            deleted += 1

        _audit(
            conn,
            actor=ACTOR_METRICS,
            action="metric_definitions_sync",
            target_table="metric_definitions",
            payload={
                "yaml_path": str(yaml_path.relative_to(REPO_ROOT)),
                "inserted": inserted,
                "updated": updated,
                "deleted": deleted,
                "total": len(metrics),
            },
        )

    return len(metrics)


def _audit(
    conn,
    *,
    actor: str,
    action: str,
    target_table: str,
    target_id: int | None = None,
    payload: dict | None = None,
) -> None:
    """Insert an audit_log row. Called from inside a mutating() block."""
    conn.execute(
        """
        INSERT INTO audit_log (ts, actor, action, target_table, target_id, payload)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (
            _now_iso(),
            actor,
            action,
            target_table,
            target_id,
            json.dumps(payload or {}, ensure_ascii=False, sort_keys=True),
        ),
    )


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
