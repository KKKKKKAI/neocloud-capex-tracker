"""The hardened claude backend, driven against a fake claude CLI."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from capex import paths, settings
from capex.adapters.cli_backend import CLIBackend, prompt_tokens
from capex.adapters.errors import (
    FATAL_LLM_ERRORS,
    LLMAuthError,
    LLMBudgetError,
    LLMConfigError,
    LLMOutputError,
    LLMTransientError,
    LLMUsageLimitError,
    classify_llm_failure,
)
from capex.db.schema import Database, migrate


@pytest.fixture
def migrated_db():
    db = Database()  # under the fake_claude fixture's CAPEX_HOME
    migrate(db)
    return db


def test_prompt_goes_over_stdin_with_hardened_flags(fake_claude, monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "must-not-leak")
    backend = CLIBackend("claude", model="claude-test-model")

    assert backend.extract("SYSTEM", "USER") == "OK"

    (call,) = fake_claude()
    assert call["stdin"] == "SYSTEM\n\nUSER"
    argv = call["argv"]
    assert argv[:3] == ["-p", "--output-format", "json"]
    assert argv[argv.index("--model") + 1] == "claude-test-model"
    assert argv[argv.index("--tools") + 1] == ""
    assert argv[argv.index("--setting-sources") + 1] == "user"
    assert "--no-session-persistence" in argv
    assert "USER" not in " ".join(argv)  # never on the command line
    assert call["cwd"] == str(paths.llm_cwd())
    assert call["cwd_entries"] == []
    assert call["env"]["ANTHROPIC_API_KEY"] is None
    assert call["env"]["DISABLE_AUTOUPDATER"] == "1"
    assert backend.last_call["input_tokens"] == 11
    assert backend.last_call["ok"] is True


def test_huge_multibyte_prompt_is_fine_over_stdin(fake_claude):
    # ~300 KB of UTF-8: far past the 128 KiB single-argument limit.
    prompt = "資本支出" * 25_000
    assert CLIBackend("claude").extract("", prompt) == "OK"
    assert fake_claude()[0]["stdin"] == prompt


@pytest.mark.parametrize("mode, error", [
    ("auth", LLMAuthError),
    ("limit", LLMUsageLimitError),
    ("model", LLMConfigError),
    ("garbage", LLMOutputError),
])
def test_failures_raise_typed_errors(fake_claude, monkeypatch, mode, error):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", mode)
    with pytest.raises(error):
        CLIBackend("claude").extract("", "hi")


def test_usage_limit_carries_the_reset_time(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "limit")
    with pytest.raises(LLMUsageLimitError) as info:
        CLIBackend("claude").extract("", "hi")
    assert info.value.resets_at == datetime.fromtimestamp(1759363200, tz=timezone.utc)


def test_timeout_is_transient(fake_claude, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "sleep")
    with pytest.raises(LLMTransientError):
        CLIBackend("claude", timeout=1).extract("", "hi")


def test_missing_binary_is_a_config_error(fake_claude, monkeypatch):
    monkeypatch.setenv("CAPEX_CLAUDE_BIN", "/nonexistent/claude")
    monkeypatch.setattr("capex.adapters.cli_backend.shutil.which", lambda name: None)
    with pytest.raises(LLMConfigError):
        CLIBackend("claude").extract("", "hi")


def test_calls_are_logged_and_budget_enforced(fake_claude, migrated_db):
    backend = CLIBackend("claude", db=migrated_db, max_calls_per_day=2)
    backend.extract("", "one")
    backend.extract("", "two")
    assert backend.calls_today() == 2
    with pytest.raises(LLMBudgetError):
        backend.extract("", "three")
    assert len(fake_claude()) == 2  # the third never reached the CLI
    with migrated_db.connect() as conn:
        rows = conn.execute("SELECT ok, prompt_chars, input_tokens FROM llm_calls").fetchall()
    assert [tuple(r) for r in rows] == [(1, 3, 11), (1, 3, 11)]


def test_failed_calls_are_logged_with_their_kind(fake_claude, migrated_db, monkeypatch):
    monkeypatch.setenv("FAKE_CLAUDE_MODE", "auth")
    with pytest.raises(LLMAuthError):
        CLIBackend("claude", db=migrated_db).extract("", "hi")
    with migrated_db.connect() as conn:
        row = conn.execute("SELECT ok, error_kind FROM llm_calls").fetchone()
    assert tuple(row) == (0, "LLMAuthError")


def test_pause_blocks_calls_until_it_expires(fake_claude):
    future = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()
    with pytest.raises(LLMUsageLimitError):
        CLIBackend("claude", paused_until=future).extract("", "hi")
    assert fake_claude() == []
    past = (datetime.now(timezone.utc) - timedelta(minutes=1)).isoformat()
    assert CLIBackend("claude", paused_until=past).extract("", "hi") == "OK"


def test_from_settings_uses_stored_settings(fake_claude, migrated_db):
    settings.set("llm.model", "claude-from-settings", db=migrated_db)
    settings.set("llm.max_calls_per_day", 5, db=migrated_db)
    backend = CLIBackend.from_settings(db=migrated_db)
    assert backend.model == "claude-from-settings"
    assert backend.max_calls_per_day == 5
    backend.extract("", "hi")
    argv = fake_claude()[0]["argv"]
    assert argv[argv.index("--model") + 1] == "claude-from-settings"


def test_no_db_file_is_created_just_to_log(fake_claude):
    backend = CLIBackend.from_settings()  # the default DB doesn't exist here
    backend.extract("", "hi")
    assert not paths.db_path().exists()


def test_auto_prefers_configured_claude(fake_claude):
    assert CLIBackend.detect_available() == "claude"
    assert CLIBackend.auto().tool == "claude"


def test_classifier_samples():
    assert isinstance(classify_llm_failure("OAuth token has expired"), LLMAuthError)
    assert isinstance(classify_llm_failure("HTTP 429 Too Many Requests"), LLMUsageLimitError)
    assert isinstance(classify_llm_failure("API Error: 529 Overloaded"), LLMTransientError)
    assert isinstance(classify_llm_failure("something odd", 3), LLMTransientError)
    for fatal in (LLMAuthError, LLMUsageLimitError, LLMBudgetError, LLMConfigError):
        assert fatal in FATAL_LLM_ERRORS
    assert LLMTransientError not in FATAL_LLM_ERRORS


UTC = timezone.utc


@pytest.mark.parametrize("message, now, resets_at", [
    # What the server saw on 2026-09-29 (it used to count as transient).
    ("You've hit your session limit · resets 7:10pm (UTC)",
     datetime(2026, 9, 29, 18, 20, tzinfo=UTC), datetime(2026, 9, 29, 19, 10, tzinfo=UTC)),
    # A clock time already passed today means tomorrow.
    ("You've hit your session limit · resets 7:10pm (UTC)",
     datetime(2026, 9, 29, 19, 30, tzinfo=UTC), datetime(2026, 9, 30, 19, 10, tzinfo=UTC)),
    ("You've hit your weekly limit · resets Oct 6, 9am (Europe/London)",
     datetime(2026, 9, 29, 18, 0, tzinfo=UTC), datetime(2026, 10, 6, 8, 0, tzinfo=UTC)),
    ("You've hit your Opus limit · resets 11pm (America/New_York)",
     datetime(2026, 9, 29, 18, 0, tzinfo=UTC), datetime(2026, 9, 30, 3, 0, tzinfo=UTC)),
    ("Claude AI usage limit reached|1759363200",
     datetime(2026, 9, 29, tzinfo=UTC), datetime.fromtimestamp(1759363200, tz=UTC)),
    ("You've hit your session limit", datetime(2026, 9, 29, tzinfo=UTC), None),
])
def test_usage_limits_pause_until_the_reset(message, now, resets_at):
    error = classify_llm_failure(message, 1, now=now)
    assert isinstance(error, LLMUsageLimitError)
    assert error.resets_at == resets_at


# ---- fatal errors stop the run instead of being swallowed -------------------

def test_router_reraises_fatal_llm_errors(monkeypatch):
    from capex.extract import router
    from capex.extract.extractors import llm_headless_filing

    monkeypatch.setattr(router, "get_extraction_chain", lambda ticker, mk: ["llm"])

    def boom(self, *args, **kwargs):
        raise LLMAuthError("token expired")

    monkeypatch.setattr(llm_headless_filing.LLMHeadlessFilingExtractor, "extract_filing", boom)
    with pytest.raises(LLMAuthError):
        router.extract_filing("MSFT", "10-Q", "2026-03-31", ["revenue"],
                              backend=object(), write=False)


def test_router_falls_back_on_ordinary_errors(monkeypatch):
    from capex.extract import router
    from capex.extract.extractors import llm_headless_filing

    monkeypatch.setattr(router, "get_extraction_chain", lambda ticker, mk: ["llm"])

    def flaky(self, *args, **kwargs):
        raise ValueError("unparseable response")

    monkeypatch.setattr(llm_headless_filing.LLMHeadlessFilingExtractor, "extract_filing", flaky)
    sentinel = object()
    monkeypatch.setattr(router, "extract_metric", lambda *a, **k: sentinel)
    out = router.extract_filing("MSFT", "10-Q", "2026-03-31", ["revenue"],
                                backend=object(), write=False)
    assert out["revenue"] is sentinel


def test_prompt_tokens_count_cached_input():
    usage = {"input_tokens": 1, "cache_creation_input_tokens": 9_500,
             "cache_read_input_tokens": 14_200, "output_tokens": 7_475}
    assert prompt_tokens(usage) == 23_701
    assert prompt_tokens({"input_tokens": 11, "output_tokens": 2}) == 11
    assert prompt_tokens({}) is None
