"""Top-level CLI entrypoint.

Phase 1 only wires up the `db` subcommand so the migrator and syncs are
runnable from a terminal. More subcommands land as each layer's Python
implementation does.

Usage:
    python -m capex.cli.main db migrate
    python -m capex.cli.main db sync-companies
    python -m capex.cli.main db sync-metrics
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    argv = argv if argv is not None else sys.argv[1:]

    if not argv or argv[0] in ("-h", "--help"):
        _print_help()
        return 0

    cmd = argv[0]
    rest = argv[1:]

    if cmd == "db":
        return _db_command(rest)

    print(f"unknown command: {cmd}", file=sys.stderr)
    _print_help()
    return 2


def _db_command(argv: list[str]) -> int:
    from capex.db import migrate
    from capex.db.sync import sync_companies, sync_metric_definitions

    if not argv:
        print("usage: capex db {migrate|sync-companies|sync-metrics|sync-all}", file=sys.stderr)
        return 2

    sub = argv[0]
    if sub == "migrate":
        version = migrate()
        print(f"schema at version {version}")
        return 0
    if sub == "sync-companies":
        n = sync_companies()
        print(f"synced {n} companies")
        return 0
    if sub == "sync-metrics":
        n = sync_metric_definitions()
        print(f"synced {n} metric definitions")
        return 0
    if sub == "sync-all":
        migrate()
        nc = sync_companies()
        nm = sync_metric_definitions()
        print(f"migrate OK; synced {nc} companies and {nm} metric definitions")
        return 0

    print(f"unknown db subcommand: {sub}", file=sys.stderr)
    return 2


def _print_help() -> None:
    print(
        "neocloud-capex-tracker CLI\n"
        "\n"
        "commands:\n"
        "    db migrate          apply pending database migrations\n"
        "    db sync-companies   refresh companies table from _identity.yaml\n"
        "    db sync-metrics     refresh metric_definitions table from YAML seed\n"
        "    db sync-all         run migrate + both syncs\n"
    )


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
