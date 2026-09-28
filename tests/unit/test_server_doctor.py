"""Server health checks, with every external call faked."""
from __future__ import annotations

import json
import smtplib

import pytest

from capex.server import doctor


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for var in ("CAPEX_CLAUDE_BIN", "ALPHA_VANTAGE_API_KEY", "GMAIL_USERNAME",
                "GMAIL_APP_PASSWORD", "CAPEX_SITE_BUCKET", "CAPEX_PUBLIC_BASE_URL",
                "CAPEX_HOME", "CAPEX_DATA_VOLUME_ID"):
        monkeypatch.delenv(var, raising=False)


# ---- claude (through the production backend, against a fake CLI) --------

def test_claude_pass_uses_the_hardened_backend(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "should-not-leak")
    result = doctor.check_claude()
    assert result.status == doctor.PASS
    (call,) = fake_claude()
    assert call["stdin"].startswith("Reply with")
    assert call["env"]["ANTHROPIC_API_KEY"] is None
    assert call["cwd_entries"] == []


def test_claude_auth_failure_is_named(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth")
    result = doctor.check_claude()
    assert result.status == doctor.FAIL
    assert "LLMAuthError" in result.detail


def test_claude_usage_limit_is_named(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    assert "LLMUsageLimitError" in doctor.check_claude().detail


def test_claude_missing_binary(monkeypatch):
    monkeypatch.setattr("capex.adapters.cli_backend.shutil.which", lambda name: None)
    result = doctor.check_claude()
    assert result.status == doctor.FAIL
    assert "not found" in result.detail


# ---- SEC / Alpha Vantage --------------------------------------------------

def test_sec_pass_and_fail(monkeypatch):
    monkeypatch.setattr(doctor, "_http_get", lambda url, headers=None, timeout=20: (200, b"{}"))
    assert doctor.check_sec().status == doctor.PASS
    monkeypatch.setattr(doctor, "_http_get", lambda url, headers=None, timeout=20: (403, b""))
    result = doctor.check_sec()
    assert result.status == doctor.FAIL
    assert "User-Agent" in result.detail


def test_alpha_vantage_counts_rows(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "AVKEY123")
    csv = b"symbol,name,reportDate\nMSFT,Microsoft,2026-10-28\nORCL,Oracle,2026-12-09\n"
    monkeypatch.setattr(doctor, "_http_get", lambda url, headers=None, timeout=20: (200, csv))
    result = doctor.check_alpha_vantage()
    assert result.status == doctor.PASS
    assert result.detail.startswith("2 upcoming")


def test_alpha_vantage_error_body_hides_the_key(monkeypatch):
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "AVKEY123")
    body = json.dumps({"Information": "Invalid apikey AVKEY123."}).encode()
    monkeypatch.setattr(doctor, "_http_get", lambda url, headers=None, timeout=20: (200, body))
    result = doctor.check_alpha_vantage()
    assert result.status == doctor.FAIL
    assert "AVKEY123" not in result.detail


def test_alpha_vantage_missing_or_demo_key(monkeypatch):
    assert doctor.check_alpha_vantage().status == doctor.FAIL
    monkeypatch.setenv("ALPHA_VANTAGE_API_KEY", "demo")
    assert doctor.check_alpha_vantage().status == doctor.FAIL


# ---- Gmail ---------------------------------------------------------------

class _FakeSMTP:
    logins: list[tuple[str, str]] = []
    reject = False

    def __init__(self, host, port, timeout):
        pass

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False

    def login(self, user, password):
        if self.reject:
            raise smtplib.SMTPAuthenticationError(535, b"bad credentials")
        _FakeSMTP.logins.append((user, password))


def test_gmail_login_strips_spaces(monkeypatch):
    monkeypatch.setenv("GMAIL_USERNAME", "ops@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "abcd efgh ijkl mnop")
    monkeypatch.setattr(_FakeSMTP, "reject", False)
    monkeypatch.setattr(doctor.smtplib, "SMTP_SSL", _FakeSMTP)
    _FakeSMTP.logins.clear()
    assert doctor.check_gmail().status == doctor.PASS
    assert _FakeSMTP.logins == [("ops@example.com", "abcdefghijklmnop")]


def test_gmail_rejected_login(monkeypatch):
    monkeypatch.setenv("GMAIL_USERNAME", "ops@example.com")
    monkeypatch.setenv("GMAIL_APP_PASSWORD", "wrong")
    monkeypatch.setattr(_FakeSMTP, "reject", True)
    monkeypatch.setattr(doctor.smtplib, "SMTP_SSL", _FakeSMTP)
    result = doctor.check_gmail()
    assert result.status == doctor.FAIL
    assert "wrong" not in result.detail


def test_gmail_not_configured():
    assert doctor.check_gmail().status == doctor.FAIL


# ---- local-only checks and the CLI ------------------------------------------

def test_site_checks_skip_without_config():
    assert doctor.check_publish().status == doctor.SKIP
    assert doctor.ensure_placeholder().status == doctor.SKIP
    assert doctor.check_data_mount().status == doctor.SKIP


def test_main_exit_codes_and_crash_handling(monkeypatch, capsys):
    monkeypatch.setitem(doctor.CHECKS, "sec", lambda: doctor.Result("sec", doctor.PASS, "ok"))
    assert doctor.main(["--only", "sec"]) == 0

    def boom():
        raise RuntimeError("kaput")

    monkeypatch.setitem(doctor.CHECKS, "sec", boom)
    assert doctor.main(["--only", "sec"]) == 1
    assert "RuntimeError: kaput" in capsys.readouterr().out


def test_main_rejects_unknown_checks():
    with pytest.raises(SystemExit):
        doctor.main(["--only", "nope"])
