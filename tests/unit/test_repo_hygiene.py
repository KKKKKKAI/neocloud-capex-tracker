"""Repository hygiene checks for a repo that lives on Windows but is
driven from WSL / Linux.

- Every tracked path must be checkout-able on Windows (NTFS): no
  reserved characters, no reserved device names, no trailing dot/space,
  no two paths that differ only by case.
- Shell scripts must be LF-only, or bash fails with `$'\\r': command
  not found` (Windows Git runs with core.autocrlf=true).
"""
from __future__ import annotations

import re
import shutil
import subprocess
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[2]

WINDOWS_ILLEGAL_CHARS = re.compile(r'[<>:"\\|?*\x00-\x1f\uf000-\uf0ff]')
WINDOWS_RESERVED_NAMES = re.compile(
    r"^(CON|PRN|AUX|NUL|COM[1-9]|LPT[1-9])(\..*)?$", re.IGNORECASE
)


def _tracked_paths() -> list[str]:
    if shutil.which("git") is None or not (REPO_ROOT / ".git").exists():
        pytest.skip("not a git checkout")
    out = subprocess.run(
        ["git", "ls-files", "-z"],
        cwd=REPO_ROOT, capture_output=True, check=True,
    ).stdout
    return [p for p in out.decode("utf-8").split("\0") if p]


def test_tracked_paths_are_windows_safe():
    bad = []
    for path in _tracked_paths():
        for part in path.split("/"):
            if (
                WINDOWS_ILLEGAL_CHARS.search(part)
                or WINDOWS_RESERVED_NAMES.match(part)
                or part.endswith((" ", "."))
            ):
                bad.append(path)
                break
    assert not bad, f"paths illegal on Windows: {bad[:10]}"


def test_no_tracked_paths_collide_case_insensitively():
    seen: dict[str, str] = {}
    collisions = []
    for path in _tracked_paths():
        key = path.lower()
        if key in seen:
            collisions.append((seen[key], path))
        seen[key] = path
    assert not collisions, f"case-insensitive collisions: {collisions}"


def test_shell_scripts_are_lf_only():
    crlf = [
        path for path in _tracked_paths()
        if path.endswith(".sh") and b"\r\n" in (REPO_ROOT / path).read_bytes()
    ]
    assert not crlf, f"CRLF line endings in shell scripts: {crlf}"


def test_gitattributes_pins_lf_for_scripts():
    rules = (REPO_ROOT / ".gitattributes").read_text(encoding="utf-8").splitlines()
    active = {line.split("#", 1)[0].strip() for line in rules}
    assert any(r.startswith("*.sh") and "eol=lf" in r for r in active)
    assert any(r.startswith("*") and "text=auto" in r and "eol=lf" in r for r in active)
