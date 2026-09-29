#!/usr/bin/env python3
"""Deploy gate: did CI pass for this exact commit?

    python3 deploy/ci_gate.py SHA [--repo OWNER/NAME]

Asks GitHub's public API for the commit's check runs (no token needed:
60 requests an hour per IP, and the deploy timer uses 6). Exit codes:
0 the `lint-and-test` check run succeeded; 1 it failed or was cancelled
(never deploy); 2 no verdict yet (pending, missing, or GitHub
unreachable/rate-limited: try again on the next timer tick).

Standard library only: it runs with the system python3 before any
release's venv exists.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

REPO = "KKKKKKAI/neocloud-capex-tracker"
CHECK_NAME = "lint-and-test"   # the CI job id; see .github/workflows/ci.yml
PASS, FAIL, WAIT = 0, 1, 2


def verdict(check_runs: list[dict], name: str = CHECK_NAME) -> tuple[int, str]:
    """(exit code, reason) from a commit's check runs (newest first)."""
    runs = [r for r in check_runs if r.get("name") == name]
    if not runs:
        return WAIT, f"no '{name}' check run yet"
    latest = max(runs, key=lambda r: r.get("started_at") or "")
    if latest.get("status") != "completed":
        return WAIT, f"'{name}' is {latest.get('status')}"
    if latest.get("conclusion") == "success":
        return PASS, f"'{name}' passed"
    return FAIL, f"'{name}' concluded {latest.get('conclusion')}"


def fetch_check_runs(sha: str, repo: str = REPO, timeout: int = 20) -> list[dict]:
    url = (f"https://api.github.com/repos/{repo}/commits/{sha}/check-runs"
           f"?check_name={CHECK_NAME}&per_page=50")
    request = urllib.request.Request(url, headers={
        "Accept": "application/vnd.github+json",
        "User-Agent": "capex-deploy",
        "X-GitHub-Api-Version": "2022-11-28",
    })
    with urllib.request.urlopen(request, timeout=timeout) as response:
        return json.load(response).get("check_runs", [])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("sha")
    parser.add_argument("--repo", default=REPO)
    args = parser.parse_args(argv)
    try:
        code, reason = verdict(fetch_check_runs(args.sha, args.repo))
    except (urllib.error.URLError, OSError, ValueError) as e:
        code, reason = WAIT, f"GitHub API unavailable: {e}"
    print(f"{args.sha[:12]}: {reason}")
    return code


if __name__ == "__main__":
    sys.exit(main())
