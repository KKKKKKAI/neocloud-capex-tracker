"""Pytest configuration shared by all test modules.

Adds the `network` marker so tests that hit live SEC EDGAR / HKEXnews
endpoints can be opted-in only when desired. By default network tests
are skipped to keep CI deterministic and offline-friendly.

Run network tests with:
    RUN_NETWORK_TESTS=1 pytest

Mark a test as network-dependent with:
    @pytest.mark.network
    def test_live_sec_msft():
        ...
"""
from __future__ import annotations

import os
import sys
from pathlib import Path

import pytest

# Make src/ importable for tests without requiring `pip install -e .`
SRC = Path(__file__).resolve().parent.parent / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def pytest_configure(config):
    config.addinivalue_line(
        "markers",
        "network: test requires live network access (SEC EDGAR, HKEXnews). "
        "Skipped unless RUN_NETWORK_TESTS=1 is set.",
    )


def pytest_collection_modifyitems(config, items):
    if os.environ.get("RUN_NETWORK_TESTS"):
        return
    import pytest

    skip_network = pytest.mark.skip(reason="network test (set RUN_NETWORK_TESTS=1 to enable)")
    for item in items:
        if "network" in item.keywords:
            item.add_marker(skip_network)


# ---- A fake `claude` CLI --------------------------------------------------

_FAKE_CLAUDE = r'''#!{python}
"""Stand-in for the claude CLI. Behaviour comes from FAKE_CLAUDE_MODE;
each call's argv, stdin, cwd and environment go to FAKE_CLAUDE_LOG."""
import json, os, sys, time

prompt = sys.stdin.read()
with open(os.environ["FAKE_CLAUDE_LOG"], "a", encoding="utf-8") as f:
    f.write(json.dumps({{
        "argv": sys.argv[1:], "stdin": prompt, "cwd": os.getcwd(),
        "cwd_entries": os.listdir("."),
        "env": {{k: os.environ.get(k) for k in (
            "ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN", "DISABLE_AUTOUPDATER",
            "CLAUDE_CODE_OAUTH_TOKEN")}},
    }}) + "\n")

mode = os.environ.get("FAKE_CLAUDE_MODE", "ok")
if mode == "ok":
    print(json.dumps({{"type": "result", "subtype": "success", "is_error": False,
                      "result": "OK", "duration_ms": 12,
                      "usage": {{"input_tokens": 11, "output_tokens": 2}}}}))
elif mode == "auth":
    print(json.dumps({{"type": "result", "is_error": True,
                      "result": "Invalid API key · Please run /login"}}))
    sys.exit(1)
elif mode == "limit":
    print(json.dumps({{"type": "result", "is_error": True,
                      "result": "Claude AI usage limit reached|1759363200"}}))
    sys.exit(1)
elif mode == "model":
    print("Error: model claude-nope not found", file=sys.stderr)
    sys.exit(1)
elif mode == "garbage":
    print("not json at all")
elif mode == "sleep":
    time.sleep(10)
'''


@pytest.fixture
def fake_claude(tmp_path, monkeypatch):
    """Install a fake claude CLI; returns a reader for the call log.

    Also points CAPEX_HOME at a scratch dir, so llm_cwd() and the default
    DB never touch the checkout. Set FAKE_CLAUDE_MODE to change behaviour.
    """
    import json

    home = tmp_path / "home"
    home.mkdir()
    monkeypatch.setenv("CAPEX_HOME", str(home))
    binary = tmp_path / "claude"
    binary.write_text(_FAKE_CLAUDE.format(python=sys.executable), encoding="utf-8")
    binary.chmod(0o755)
    log = tmp_path / "claude-calls.jsonl"
    monkeypatch.setenv("CAPEX_CLAUDE_BIN", str(binary))
    monkeypatch.setenv("FAKE_CLAUDE_LOG", str(log))
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "ok")

    def calls() -> list[dict]:
        if not log.exists():
            return []
        return [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]

    return calls
