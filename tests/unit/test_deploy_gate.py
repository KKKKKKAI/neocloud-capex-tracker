"""The deploy's CI gate (deploy/ci_gate.py) and the systemd units' contract."""
from __future__ import annotations

import importlib.util
import re

import pytest

from capex import paths

spec = importlib.util.spec_from_file_location("ci_gate", paths.CODE_ROOT / "deploy" / "ci_gate.py")
ci_gate = importlib.util.module_from_spec(spec)
spec.loader.exec_module(ci_gate)


def _run(status, conclusion=None, started="2026-10-01T10:00:00Z", name="lint-and-test"):
    return {"name": name, "status": status, "conclusion": conclusion, "started_at": started}


@pytest.mark.parametrize("runs, code", [
    ([_run("completed", "success")], ci_gate.PASS),
    ([_run("completed", "failure")], ci_gate.FAIL),
    ([_run("completed", "cancelled")], ci_gate.FAIL),
    ([_run("in_progress")], ci_gate.WAIT),
    ([], ci_gate.WAIT),
    ([_run("completed", "success", name="something-else")], ci_gate.WAIT),
    # a re-run decides: the newest run wins
    ([_run("completed", "failure", "2026-10-01T10:00:00Z"),
      _run("completed", "success", "2026-10-01T11:00:00Z")], ci_gate.PASS),
])
def test_verdict(runs, code):
    assert ci_gate.verdict(runs)[0] == code


def test_github_outage_means_wait(monkeypatch, capsys):
    def down(*a, **kw):
        raise OSError("network unreachable")
    monkeypatch.setattr(ci_gate, "fetch_check_runs", down)
    assert ci_gate.main(["0123456789abcdef"]) == ci_gate.WAIT
    assert "unavailable" in capsys.readouterr().out


def test_the_gate_names_the_ci_job():
    workflow = (paths.CODE_ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
    assert re.search(rf"^  {ci_gate.CHECK_NAME}:\s*$", workflow, re.MULTILINE)


def test_units_run_commands_that_exist():
    """Every capex command a unit runs is a real CLI command."""
    from capex.cli.server import server_command

    units = sorted((paths.CODE_ROOT / "deploy" / "systemd").glob("*.service"))
    assert {u.name for u in units} >= {"capex-scheduler.service", "capex-admin.service",
                                       "capex-secrets.service", "capex-deploy.service",
                                       "capex-alert@.service"}
    for unit in units:
        for line in unit.read_text(encoding="utf-8").splitlines():
            if not line.startswith("ExecStart=/opt/capex/current/.venv/bin/capex server"):
                continue
            subcommand = line.split()[2]
            with pytest.raises(SystemExit) as exit_info:
                server_command([subcommand, "--help"])
            assert exit_info.value.code == 0, f"{unit.name}: capex server {subcommand}"
