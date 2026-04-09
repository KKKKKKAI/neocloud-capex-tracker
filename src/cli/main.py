"""Top-level CLI entrypoint (placeholder).

Intended subcommands (not yet implemented):

    neocloud-capex watch           # run the watcher layer once
    neocloud-capex ingest <url>    # ingest a single filing by URL
    neocloud-capex extract <id>    # extract from an ingested document
    neocloud-capex validate <id>   # run the validation pipeline
    neocloud-capex run             # end-to-end: watch -> ingest -> extract -> validate -> workbook
"""
from __future__ import annotations

import sys


def main(argv: list[str] | None = None) -> int:
    print("neocloud-capex-tracker: CLI not yet implemented.")
    print("See docs/SYSTEM_DESIGN.md for the current architecture.")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
