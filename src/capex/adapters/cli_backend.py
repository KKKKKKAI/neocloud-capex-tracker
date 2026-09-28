"""LLM backend that shells out to a CLI in print mode.

`claude` (Claude Code, authenticated by CLAUDE_CODE_OAUTH_TOKEN on the
server) is the production path:

- the prompt goes over stdin, not argv (a ~106 KB filing prompt is close
  to Linux's 128 KiB per-argument limit);
- `--output-format json`, parsed for `result` / `is_error` / `usage`;
- `--tools ""`: text in, text out, no tool use;
- runs in an empty working directory with `--setting-sources user`, so a
  repository CLAUDE.md or .claude/settings are never loaded;
- ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN are removed from the child
  environment, so the subscription token is always the credential used;
- failures raise the typed errors in adapters/errors.py (auth, usage
  limit, budget, config, transient, output);
- with a `db`, every call is logged to `llm_calls`, and the daily budget
  (setting llm.max_calls_per_day) and pause (llm.paused_until) apply.

`gemini` and `codex` keep the original argv-prompt behaviour.

Usage:
    from capex.adapters.cli_backend import CLIBackend

    backend = CLIBackend.from_settings()   # claude, configured from settings
    backend = CLIBackend.auto()            # first installed CLI
    response = backend.extract("system prompt", "user prompt")
"""
from __future__ import annotations

import json
import os
import shutil
import sqlite3
import subprocess
import time
from datetime import datetime, timezone
from typing import Any

from .. import paths
from .errors import (
    LLMBudgetError,
    LLMConfigError,
    LLMError,
    LLMOutputError,
    LLMTransientError,
    LLMUsageLimitError,
    classify_llm_failure,
)

DEFAULT_CLAUDE_MODEL = "claude-opus-4-8"

# Argv-prompt CLIs kept for local experiments.
LEGACY_TOOLS: dict[str, dict[str, Any]] = {
    "gemini": {"cmd": "gemini", "args": ["-p"]},
    "codex": {"cmd": "codex", "args": ["-p"]},
}
TOOLS = ("claude", *LEGACY_TOOLS)


def claude_binary() -> str | None:
    """$CAPEX_CLAUDE_BIN if executable, else `claude` on PATH."""
    configured = os.environ.get("CAPEX_CLAUDE_BIN")
    if configured and os.access(configured, os.X_OK):
        return configured
    return shutil.which("claude")


def llm_child_env() -> dict[str, str]:
    """Environment for the CLI: no API-key variables (so the subscription
    token is used) and no self-updates mid-run."""
    env = {
        k: v for k, v in os.environ.items()
        if k not in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")
    }
    env["DISABLE_AUTOUPDATER"] = "1"
    return env


def last_json_object(text: str) -> dict | None:
    """The last line of `text` that parses as a JSON object."""
    for line in reversed(text.strip().splitlines()):
        try:
            obj = json.loads(line)
        except ValueError:
            continue
        if isinstance(obj, dict):
            return obj
    return None


class CLIBackend:
    """Model backend that calls an LLM CLI in print mode.

    Implements the ModelBackend protocol from adapters/base.py.
    """

    name: str
    version: str = "cli-2.0"

    def __init__(
        self,
        tool: str = "claude",
        *,
        timeout: int = 300,
        model: str | None = None,
        fallback_model: str | None = None,
        binary: str | None = None,
        db: Any | None = None,
        max_calls_per_day: int | None = None,
        paused_until: str | None = None,
    ) -> None:
        if tool not in TOOLS:
            raise ValueError(f"Unknown CLI tool {tool!r}. Available: {list(TOOLS)}")
        self.tool = tool
        self.name = f"{tool}-cli"
        self.timeout = timeout
        self.model = model or (DEFAULT_CLAUDE_MODEL if tool == "claude" else None)
        self.fallback_model = fallback_model or None
        self.binary = binary or (
            claude_binary() if tool == "claude" else LEGACY_TOOLS[tool]["cmd"]
        )
        self.db = db
        self.max_calls_per_day = max_calls_per_day
        self.paused_until = paused_until or None
        self.last_call: dict[str, Any] | None = None

    # ---- construction ---------------------------------------------------
    @classmethod
    def from_settings(cls, db: Any | None = None, **overrides: Any) -> CLIBackend:
        """A claude backend configured from the `llm.*` settings."""
        from .. import settings
        from ..db import Database

        db = db or Database()
        kwargs: dict[str, Any] = {
            "model": settings.get("llm.model", db),
            "fallback_model": settings.get("llm.fallback_model", db),
            "timeout": settings.get("llm.timeout_s", db),
            "max_calls_per_day": settings.get("llm.max_calls_per_day", db),
            "paused_until": settings.get("llm.paused_until", db),
            "db": db,
        }
        kwargs.update(overrides)
        return cls("claude", **kwargs)

    @classmethod
    def auto(cls, db: Any | None = None, **kwargs: Any) -> CLIBackend:
        """The first installed CLI (claude preferred, configured from settings)."""
        tool = cls.detect_available()
        if tool is None:
            raise LLMConfigError(
                "No LLM CLI tool found. Install Claude Code (or set "
                "CAPEX_CLAUDE_BIN), gemini, or codex."
            )
        if tool == "claude":
            return cls.from_settings(db=db, **kwargs)
        return cls(tool, **{k: v for k, v in kwargs.items() if k == "timeout"})

    @staticmethod
    def detect_available() -> str | None:
        """Name of the first available CLI tool, or None."""
        if claude_binary():
            return "claude"
        for tool, config in LEGACY_TOOLS.items():
            if shutil.which(config["cmd"]):
                return tool
        return None

    @staticmethod
    def list_available() -> list[str]:
        """All installed CLI tools."""
        found = ["claude"] if claude_binary() else []
        return found + [t for t, c in LEGACY_TOOLS.items() if shutil.which(c["cmd"])]

    # ---- calls ------------------------------------------------------------
    def extract(
        self,
        system: str,
        user: str,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        """Send the prompt, return the model's text.

        System and user are concatenated: print mode has no separate
        roles. Raises an LLMError subclass on failure.
        """
        prompt = f"{system}\n\n{user}" if system else user
        if self.tool != "claude":
            return self._extract_legacy(prompt)
        self._check_allowed()
        return self._extract_claude(prompt)

    def calls_today(self) -> int:
        """LLM calls logged since 00:00 UTC (0 without a usable DB)."""
        db = self._telemetry_db()
        if db is None:
            return 0
        start = datetime.now(timezone.utc).strftime("%Y-%m-%dT00:00:00")
        try:
            with db.connect() as conn:
                return conn.execute(
                    "SELECT COUNT(*) FROM llm_calls WHERE ts >= ?", (start,)
                ).fetchone()[0]
        except sqlite3.Error:
            return 0

    def _check_allowed(self) -> None:
        if self.paused_until:
            until = datetime.fromisoformat(self.paused_until)
            if until.tzinfo is None:
                until = until.replace(tzinfo=timezone.utc)
            if datetime.now(timezone.utc) < until:
                raise LLMUsageLimitError(
                    f"LLM calls paused until {until.isoformat()} (setting llm.paused_until)",
                    resets_at=until,
                )
        if self.max_calls_per_day is not None and self.db is not None:
            used = self.calls_today()
            if used >= self.max_calls_per_day:
                raise LLMBudgetError(
                    f"daily LLM budget used: {used}/{self.max_calls_per_day} calls "
                    "(setting llm.max_calls_per_day)"
                )

    def _extract_claude(self, prompt: str) -> str:
        if not self.binary:
            raise LLMConfigError(
                "claude CLI not found (install Claude Code or set CAPEX_CLAUDE_BIN)"
            )
        cmd = [
            self.binary, "-p", "--output-format", "json", "--model", self.model,
            "--tools", "", "--no-session-persistence", "--disable-slash-commands",
            "--setting-sources", "user",
        ]
        if self.fallback_model:
            cmd += ["--fallback-model", self.fallback_model]

        started = time.monotonic()
        try:
            proc = subprocess.run(
                cmd, input=prompt, capture_output=True, text=True,
                timeout=self.timeout, cwd=str(paths.llm_cwd()), env=llm_child_env(),
            )
        except subprocess.TimeoutExpired as e:
            error = LLMTransientError(f"claude timed out after {self.timeout}s")
            self._record(prompt, started, None, error)
            raise error from e
        except OSError as e:
            raise LLMConfigError(f"could not run {self.binary}: {e.strerror}") from e

        envelope = last_json_object(proc.stdout)
        if envelope is not None and proc.returncode == 0 and not envelope.get("is_error"):
            result = envelope.get("result")
            if isinstance(result, str):
                self._record(prompt, started, envelope, None)
                return result
            error: LLMError = LLMOutputError("claude returned no text result")
        elif envelope is None and proc.returncode == 0:
            # The CLI "succeeded" but didn't speak JSON: a retry won't help.
            error = LLMOutputError("claude printed no JSON result")
        else:
            text = str((envelope or {}).get("result") or proc.stderr or proc.stdout or "")
            error = (
                classify_llm_failure(text, proc.returncode) if text.strip()
                else LLMOutputError(f"claude exited {proc.returncode} with no output")
            )
        self._record(prompt, started, envelope, error)
        raise error

    def _extract_legacy(self, prompt: str) -> str:
        config = LEGACY_TOOLS[self.tool]
        cmd = [self.binary, *config["args"], prompt]
        try:
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=self.timeout)
        except subprocess.TimeoutExpired as e:
            raise LLMTransientError(f"{self.tool} CLI timed out after {self.timeout}s") from e
        except FileNotFoundError as e:
            raise LLMConfigError(f"{self.tool} CLI not found") from e
        if result.returncode != 0:
            raise classify_llm_failure(result.stderr or result.stdout, result.returncode)
        return result.stdout

    # ---- telemetry ----------------------------------------------------------
    def _telemetry_db(self) -> Any | None:
        # Never create a DB file just to log: skip until one exists.
        if self.db is None or not self.db.path.exists():
            return None
        return self.db

    def _record(self, prompt: str, started: float, envelope: dict | None,
                error: LLMError | None) -> None:
        usage = (envelope or {}).get("usage") or {}
        self.last_call = {
            "ts": datetime.now(timezone.utc).isoformat(timespec="seconds"),
            "model": self.model,
            "prompt_chars": len(prompt),
            "input_tokens": usage.get("input_tokens"),
            "output_tokens": usage.get("output_tokens"),
            "duration_ms": int((time.monotonic() - started) * 1000),
            "ok": error is None,
            "error_kind": type(error).__name__ if error else None,
        }
        db = self._telemetry_db()
        if db is None:
            return
        c = self.last_call
        try:
            # Telemetry, not system-of-record data: skip the dump.sql hook.
            with db.connect() as conn:
                conn.execute(
                    "INSERT INTO llm_calls (ts, backend, model, prompt_chars, "
                    "input_tokens, output_tokens, duration_ms, ok, error_kind) "
                    "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (c["ts"], self.name, c["model"], c["prompt_chars"],
                     c["input_tokens"], c["output_tokens"], c["duration_ms"],
                     int(c["ok"]), c["error_kind"]),
                )
                conn.commit()
        except sqlite3.Error:
            pass  # logging must never break an extraction

    def __repr__(self) -> str:
        return f"CLIBackend(tool={self.tool!r}, model={self.model!r})"
