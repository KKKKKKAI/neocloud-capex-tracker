"""Errors raised by LLM backends, split by what the caller should do.

Fatal errors (FATAL_LLM_ERRORS) mean *every* further call will fail the
same way — an expired token, a hit usage limit, a missing CLI, an
exhausted daily budget. Callers must stop the run and surface them
instead of treating them as "the model found nothing". Transient and
output errors concern a single call and may be retried or skipped.

All subclass RuntimeError, so pre-existing `except RuntimeError`
handlers keep working.
"""
from __future__ import annotations

import re
from datetime import datetime, timezone


class LLMError(RuntimeError):
    """Base class for every backend failure."""


class LLMAuthError(LLMError):
    """Credentials missing, invalid or expired (e.g. the OAuth token)."""


class LLMUsageLimitError(LLMError):
    """Subscription usage limit hit, or calls paused until a time."""

    def __init__(self, message: str, resets_at: datetime | None = None) -> None:
        super().__init__(message)
        self.resets_at = resets_at


class LLMBudgetError(LLMError):
    """Our own daily call cap (setting llm.max_calls_per_day) is used up."""


class LLMConfigError(LLMError):
    """The backend can't run: CLI not installed, model unavailable, ..."""


class LLMTransientError(LLMError):
    """One call failed (timeout, overload, network); a retry may work."""


class LLMOutputError(LLMError):
    """The call returned, but not in a usable shape."""


FATAL_LLM_ERRORS: tuple[type[LLMError], ...] = (
    LLMAuthError, LLMUsageLimitError, LLMBudgetError, LLMConfigError,
)

_AUTH = re.compile(
    r"\b401\b|unauthori[sz]ed|authenticat|invalid (api key|token|bearer)|"
    r"/login\b|login expired|token (has )?expired|oauth token",
    re.IGNORECASE,
)
_USAGE_LIMIT = re.compile(
    r"usage limit|limit reached|rate.?limit|\b429\b|too many requests", re.IGNORECASE,
)
_MODEL = re.compile(
    r"model[^\n]{0,40}(not found|not available|does not exist|invalid)|"
    r"invalid model|unknown model",
    re.IGNORECASE,
)
_TRANSIENT = re.compile(
    r"overloaded|\b5\d\d\b|timed? ?out|connection|network|temporar|try again",
    re.IGNORECASE,
)
# Older CLI builds report "Claude AI usage limit reached|<unix epoch>".
_RESET_EPOCH = re.compile(r"\|(\d{10})\b")


def classify_llm_failure(text: str, returncode: int | None = None) -> LLMError:
    """Map a failed call's message (never containing secrets) to an error."""
    first_line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    detail = first_line[:200] or f"exit status {returncode}"
    if _AUTH.search(text):
        return LLMAuthError(
            f"authentication failed (CLAUDE_CODE_OAUTH_TOKEN missing, invalid or "
            f"expired): {detail}"
        )
    if _USAGE_LIMIT.search(text):
        resets_at = None
        m = _RESET_EPOCH.search(text)
        if m:
            resets_at = datetime.fromtimestamp(int(m.group(1)), tz=timezone.utc)
        return LLMUsageLimitError(f"usage limit reached: {detail}", resets_at=resets_at)
    if _MODEL.search(text):
        return LLMConfigError(f"model unavailable: {detail}")
    if _TRANSIENT.search(text):
        return LLMTransientError(detail)
    return LLMTransientError(f"exit {returncode}: {detail}")
