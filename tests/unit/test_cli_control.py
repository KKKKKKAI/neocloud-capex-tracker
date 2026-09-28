"""`capex llm ...` and `capex settings ...`."""
from __future__ import annotations

import pytest

from capex.cli.main import main
from capex.db.schema import Database, migrate


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    migrate(Database())
    return tmp_path


def test_settings_set_get_list(home, capsys):
    assert main(["settings", "set", "llm.max_calls_per_day", "80"]) == 0
    assert main(["settings", "get", "llm.max_calls_per_day"]) == 0
    assert main(["settings", "list"]) == 0
    out = capsys.readouterr().out
    assert "llm.max_calls_per_day = 80" in out
    assert "* llm.max_calls_per_day" in out  # marked as changed
    assert "  llm.model" in out              # untouched default


def test_settings_rejects_bad_input(home, capsys):
    assert main(["settings", "set", "llm.max_calls_per_day", "lots"]) == 2
    assert main(["settings", "set", "no.such", "1"]) == 2
    assert main(["settings", "frobnicate"]) == 2
    assert "unknown setting" in capsys.readouterr().err


def test_settings_reset(home, capsys):
    main(["settings", "set", "llm.model", "claude-x"])
    assert main(["settings", "reset", "llm.model"]) == 0
    assert "reset to \"claude-opus-4-8\"" in capsys.readouterr().out


def test_settings_on_an_unmigrated_db_says_so(tmp_path, monkeypatch, capsys):
    monkeypatch.setenv("CAPEX_HOME", str(tmp_path))
    (tmp_path / "data" / "db").mkdir(parents=True)
    Database().path.touch()  # exists, but no tables
    assert main(["settings", "set", "llm.model", "claude-x"]) == 1
    assert "capex db migrate" in capsys.readouterr().err


@pytest.mark.parametrize("mode, code", [
    ("ok", 0), ("auth", 77), ("limit", 75), ("garbage", 1),
])
def test_llm_ping_exit_codes(fake_claude, monkeypatch, mode, code):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    assert main(["llm", "ping"]) == code


def test_llm_ping_model_override(fake_claude, capsys):
    assert main(["llm", "ping", "--model", "claude-sonnet-5"]) == 0
    argv = fake_claude()[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-sonnet-5"
    assert "claude-sonnet-5" in capsys.readouterr().out


def test_llm_usage_reports_budget(fake_claude, capsys):
    migrate(Database())
    main(["llm", "ping"])
    assert main(["llm", "usage"]) == 0
    assert "1 calls, 0 failed, budget 150" in capsys.readouterr().out
