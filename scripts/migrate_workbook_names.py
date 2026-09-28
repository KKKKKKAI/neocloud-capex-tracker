"""Rename legacy workbooks to the Windows-safe filename format.

    [2026.08.14 - 11:03] financials sourcebook v2.xlsx
 -> [2026.08.14 - 11h03] financials sourcebook v2.xlsx

A ':' is illegal on NTFS. WSL stores it as U+F03A, so Windows Git sees
every legacy workbook as deleted + untracked. Run this from WSL.

Usage:
    python scripts/migrate_workbook_names.py               # dry run
    python scripts/migrate_workbook_names.py --git         # `git mv` tracked files
    python scripts/migrate_workbook_names.py --copy-to DIR # copy, renamed, into DIR
"""
from __future__ import annotations

import argparse
import shutil
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from capex.exporters.excel import (  # noqa: E402
    WORKBOOK_DIR,
    format_workbook_name,
    parse_workbook_name,
)


def plan_renames(workbook_dir: Path) -> list[tuple[Path, Path]]:
    """(old, new) pairs for every workbook whose name isn't canonical.

    Files already in the canonical form map to themselves, so `--copy-to`
    can carry them over too.
    """
    pairs = []
    for path in sorted(workbook_dir.iterdir()):
        parsed = parse_workbook_name(path.name)
        if parsed is None:
            continue
        pairs.append((path, workbook_dir / format_workbook_name(*parsed)))
    targets = [new for _, new in pairs]
    dupes = {t for t in targets if targets.count(t) > 1}
    if dupes:
        raise SystemExit(f"rename would collide: {sorted(d.name for d in dupes)}")
    return pairs


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument("--git", action="store_true", help="rename with `git mv`")
    mode.add_argument("--copy-to", type=Path, metavar="DIR", help="copy renamed files into DIR")
    parser.add_argument("--dir", type=Path, default=WORKBOOK_DIR, help="workbook directory")
    args = parser.parse_args(argv)

    pairs = plan_renames(args.dir)
    renames = [(old, new) for old, new in pairs if old != new]
    print(f"{len(pairs)} workbooks, {len(renames)} need renaming")

    if args.copy_to:
        args.copy_to.mkdir(parents=True, exist_ok=True)
        for old, new in pairs:
            shutil.copy2(old, args.copy_to / new.name)
        print(f"copied {len(pairs)} workbooks into {args.copy_to}")
        return 0

    for old, new in renames:
        print(f"  {old.name}  ->  {new.name}")
        if args.git:
            subprocess.run(
                ["git", "mv", "--", str(old.relative_to(REPO_ROOT)),
                 str(new.relative_to(REPO_ROOT))],
                cwd=REPO_ROOT, check=True,
            )
    if renames and not args.git:
        print("dry run — pass --git to rename tracked files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
