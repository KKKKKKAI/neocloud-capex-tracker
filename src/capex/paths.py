"""Where the tracker's code, config and runtime data live.

Code and hand-edited config (seed YAML, the company registry, prompts,
migrations, scripts) ship with the checkout: `CODE_ROOT`.

Everything the pipeline *writes* — the SQLite DB, raw filings,
workbooks, charts, the generated site, reports, logs, backups — lives
under the runtime home: `$CAPEX_HOME`, defaulting to the checkout. A
laptop checkout therefore behaves exactly as before, while the server
points CAPEX_HOME at its data volume (/var/lib/capex).

Runtime locations are functions, not constants, so they follow
CAPEX_HOME at call time (tests and scripts can switch homes).

DB rows store raw filings as `data/_sources/<T>/_raw/<name>`: a POSIX
path relative to the home. `raw_path_key()` builds that form and
`resolve_raw_path()` maps it back to a file.
"""
from __future__ import annotations

import os
from pathlib import Path, PurePosixPath

CODE_ROOT = Path(__file__).resolve().parents[2]

# A server release directory carries this marker; data must never be
# written into it, so CAPEX_HOME is mandatory there.
RELEASE_MARKER = ".capex-release"

# ---- Code and hand-edited config (always under the checkout) ----------
SEEDS_DIR = CODE_ROOT / "data" / "seeds"
COVERAGE_YAML = SEEDS_DIR / "coverage.yaml"
METRIC_DEFINITIONS_YAML = SEEDS_DIR / "metric_definitions.yaml"
AUDIT_BOUNDS_YAML = SEEDS_DIR / "audit_bounds.yaml"
HUMAN_NOTES_YAML = SEEDS_DIR / "human_notes.yaml"
IDENTITY_YAML = CODE_ROOT / "data" / "_sources" / "_identity.yaml"
SCRIPTS_DIR = CODE_ROOT / "scripts"


# ---- Runtime data (under the home) -------------------------------------
def home() -> Path:
    """The runtime home: $CAPEX_HOME, else the checkout."""
    configured = os.environ.get("CAPEX_HOME")
    if configured:
        return Path(configured)
    if (CODE_ROOT / RELEASE_MARKER).exists():
        raise RuntimeError(
            "CAPEX_HOME is not set; refusing to write data into a server "
            f"release directory ({CODE_ROOT})"
        )
    return CODE_ROOT


def data_dir() -> Path:
    return home() / "data"


def db_path() -> Path:
    """The SQLite system of record ($CAPEX_DB_PATH overrides)."""
    override = os.environ.get("CAPEX_DB_PATH")
    return Path(override) if override else data_dir() / "db" / "capex.db"


def dump_path() -> Path:
    """The SQL dump that sits next to the canonical DB."""
    return db_path().with_name("dump.sql")


def sources_dir() -> Path:
    """Raw filing archive: sources_dir()/<TICKER>/_raw/<file>."""
    return data_dir() / "_sources"


def local_dir() -> Path:
    """Private, never-committed runtime files (e.g. subscribers.yaml)."""
    return data_dir() / "_local"


def workbook_dir() -> Path:
    return home() / "workbook"


def charts_dir() -> Path:
    return home() / "charts"


def site_dir() -> Path:
    """Generated public site (dashboard + chart pages)."""
    return home() / "site"


def output_dir() -> Path:
    """Reports such as data_quality_report.{md,json}."""
    return home() / "output"


def logs_dir() -> Path:
    return home() / "logs"


def backups_dir() -> Path:
    return home() / "backups"


def run_dir() -> Path:
    """Locks, heartbeats and other small runtime state."""
    return home() / "run"


def llm_cwd() -> Path:
    """An empty working directory for LLM CLI calls, so no repository
    CLAUDE.md or .claude settings are picked up."""
    path = home() / "llm-cwd"
    path.mkdir(parents=True, exist_ok=True)
    return path


# ---- Raw filing paths as stored in the DB ------------------------------
def raw_path_key(path: Path) -> str:
    """DB form of a file under the home: POSIX, relative to the home."""
    rel = Path(path).resolve().relative_to(home().resolve())
    return PurePosixPath(*rel.parts).as_posix()


def resolve_raw_path(stored: str | None) -> Path | None:
    """The file behind a DB `raw_path`, or None when there is none.

    `scheme://…` values (6k://, xbrl://, restated-virtual://) are
    virtual rows with no file. Relative values resolve against the home;
    absolute ones are used as-is. Callers still check `.exists()`.
    """
    if not stored or "://" in stored:
        return None
    path = Path(stored.replace("\\", "/"))
    return path if path.is_absolute() else home() / path
