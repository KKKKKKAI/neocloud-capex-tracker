"""CLI backend adapter — calls LLM via Claude Code, Gemini CLI, or Codex CLI.

Uses -p (print) mode only: prompt in → text out → exit.
No tool permissions, no file access, no bash execution by the LLM.
This means the pipeline runs unattended via cron without approval prompts.

Usage:
    from capex.adapters.cli_backend import CLIBackend

    backend = CLIBackend("claude")         # explicit
    backend = CLIBackend.auto()            # auto-detect installed CLI
    response = backend.extract("system prompt", "user prompt")
"""
from __future__ import annotations

import shutil
import subprocess
from typing import Any

# CLI tool configurations
# Each tool uses a "print" flag that sends a prompt and returns text to stdout.
TOOL_CONFIGS = {
    "claude": {
        "cmd": "claude",
        "args": ["-p"],          # claude -p "prompt"
        "version_flag": "--version",
    },
    "gemini": {
        "cmd": "gemini",
        "args": ["-p"],
        "version_flag": "--version",
    },
    "codex": {
        "cmd": "codex",
        "args": ["-p"],
        "version_flag": "--version",
    },
}


class CLIBackend:
    """Model backend that calls LLM CLI tools in print mode.

    Implements the ModelBackend protocol from adapters/base.py.
    The LLM receives a prompt string and returns text to stdout.
    No interactive session, no tool use, no permissions needed.
    """

    name: str
    version: str = "cli-1.0"

    def __init__(self, tool: str = "claude", *, timeout: int = 300):
        """Initialize with a specific CLI tool.

        Args:
            tool: one of "claude", "gemini", "codex"
            timeout: max seconds to wait for LLM response
        """
        if tool not in TOOL_CONFIGS:
            raise ValueError(
                f"Unknown CLI tool {tool!r}. "
                f"Available: {list(TOOL_CONFIGS.keys())}"
            )
        self.tool = tool
        self.config = TOOL_CONFIGS[tool]
        self.timeout = timeout
        self.name = f"{tool}-cli"

    def extract(
        self,
        system: str,
        user: str,
        response_schema: dict[str, Any] | None = None,
    ) -> str:
        """Send prompt to CLI tool, return stdout text.

        Combines system + user into a single prompt string since CLI
        tools in print mode don't have separate system/user roles.
        """
        # Build the full prompt
        if system:
            prompt = f"{system}\n\n{user}"
        else:
            prompt = user

        # Call the CLI tool
        cmd = [self.config["cmd"], *self.config["args"], prompt]

        try:
            result = subprocess.run(
                cmd,
                capture_output=True,
                text=True,
                timeout=self.timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RuntimeError(
                f"{self.tool} CLI timed out after {self.timeout}s"
            ) from e
        except FileNotFoundError as e:
            raise RuntimeError(
                f"{self.tool} CLI not found. "
                f"Install it or use a different backend."
            ) from e

        if result.returncode != 0:
            stderr = result.stderr.strip()[:500]
            raise RuntimeError(
                f"{self.tool} CLI exited with code {result.returncode}: "
                f"{stderr}"
            )

        return result.stdout

    @classmethod
    def auto(cls, **kwargs: Any) -> CLIBackend:
        """Auto-detect the first available CLI tool and return a backend.

        Checks claude → gemini → codex in order.
        Raises RuntimeError if none are installed.
        """
        tool = cls.detect_available()
        if tool is None:
            raise RuntimeError(
                "No LLM CLI tool found. Install one of: "
                "claude (Claude Code), gemini (Gemini CLI), codex (Codex CLI)"
            )
        return cls(tool, **kwargs)

    @staticmethod
    def detect_available() -> str | None:
        """Return the name of the first available CLI tool, or None."""
        for tool, config in TOOL_CONFIGS.items():
            if shutil.which(config["cmd"]):
                return tool
        return None

    @staticmethod
    def list_available() -> list[str]:
        """Return all installed CLI tools."""
        return [
            tool for tool, config in TOOL_CONFIGS.items()
            if shutil.which(config["cmd"])
        ]

    def __repr__(self) -> str:
        return f"CLIBackend(tool={self.tool!r})"
