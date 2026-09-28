"""`capex llm` and `capex settings`: runtime control from the command line.

    capex llm ping [--model MODEL]   one real call through the production backend
    capex llm usage                  calls today vs the daily budget, recent failures
    capex settings list              every setting (* = changed from the default)
    capex settings help              every setting with its description
    capex settings get KEY
    capex settings set KEY VALUE     lists: comma-separated or JSON; dicts: JSON
    capex settings reset KEY         back to the default

`capex llm ping` exit codes: 0 ok, 1 error, 75 deferred (usage limit,
budget or pause), 77 authentication failed.
"""
from __future__ import annotations

import getpass
import json
import sqlite3
import sys

EXIT_OK, EXIT_ERROR, EXIT_USAGE = 0, 1, 2
EXIT_LLM_DEFERRED = 75  # EX_TEMPFAIL: usage limit, budget, pause
EXIT_LLM_AUTH = 77      # EX_NOPERM: token missing, invalid or expired

PING_PROMPT = "Reply with the single word OK."


def _usage(text: str) -> int:
    print(text.strip("\n"), file=sys.stderr)
    return EXIT_USAGE


def _actor() -> str:
    try:
        return f"cli:{getpass.getuser()}"
    except OSError:
        return "cli"


# ---- capex llm ----------------------------------------------------------

def llm_command(argv: list[str]) -> int:
    if argv and argv[0] == "ping":
        return _llm_ping(argv[1:])
    if argv and argv[0] == "usage":
        return _llm_usage()
    return _usage("usage: capex llm ping [--model MODEL] | capex llm usage")


def _llm_ping(argv: list[str]) -> int:
    from ..adapters.cli_backend import CLIBackend
    from ..adapters.errors import (
        LLMAuthError,
        LLMBudgetError,
        LLMError,
        LLMUsageLimitError,
    )

    overrides = {}
    if "--model" in argv:
        i = argv.index("--model")
        if i + 1 >= len(argv):
            return _usage("--model needs a value")
        overrides["model"] = argv[i + 1]
    backend = CLIBackend.from_settings(**overrides)
    try:
        answer = backend.extract("", PING_PROMPT)
    except LLMAuthError as e:
        print(f"auth failed: {e}", file=sys.stderr)
        return EXIT_LLM_AUTH
    except (LLMUsageLimitError, LLMBudgetError) as e:
        print(f"deferred: {e}", file=sys.stderr)
        return EXIT_LLM_DEFERRED
    except LLMError as e:
        print(f"{type(e).__name__}: {e}", file=sys.stderr)
        return EXIT_ERROR
    call = backend.last_call or {}
    print(
        f"{backend.model}: {answer.strip()[:80]!r} in {call.get('duration_ms')} ms "
        f"(tokens in={call.get('input_tokens')} out={call.get('output_tokens')})"
    )
    return EXIT_OK if "OK" in answer.upper() else EXIT_ERROR


def _llm_usage() -> int:
    from .. import settings
    from ..db import Database

    db = Database()
    budget = settings.get("llm.max_calls_per_day", db)
    try:
        with db.connect() as conn:
            today = conn.execute(
                "SELECT COUNT(*), SUM(ok = 0) FROM llm_calls "
                "WHERE ts >= strftime('%Y-%m-%dT00:00:00', 'now')"
            ).fetchone()
            recent = conn.execute(
                "SELECT ts, model, error_kind FROM llm_calls WHERE ok = 0 "
                "ORDER BY id DESC LIMIT 5"
            ).fetchall()
    except sqlite3.OperationalError:
        print("no llm_calls table yet: run `capex db migrate`", file=sys.stderr)
        return EXIT_ERROR
    print(f"today (UTC): {today[0]} calls, {today[1] or 0} failed, budget {budget}")
    paused = settings.get("llm.paused_until", db)
    if paused:
        print(f"paused until: {paused}")
    for row in recent:
        print(f"  failed {row['ts']}  {row['model']}  {row['error_kind']}")
    return EXIT_OK


# ---- capex settings ---------------------------------------------------------

SETTINGS_USAGE = """
usage: capex settings list | help | get KEY | set KEY VALUE | reset KEY
"""


def settings_command(argv: list[str]) -> int:
    from .. import settings

    sub, rest = (argv[0], argv[1:]) if argv else ("", [])
    try:
        if sub == "list" and not rest:
            for s, value, is_default in settings.all_settings():
                print(f"{' ' if is_default else '*'} {s.key:28} {json.dumps(value)}")
            print("\n* = changed from the default")
            return EXIT_OK
        if sub == "help" and not rest:
            for s in settings.REGISTRY.values():
                print(f"{s.key}  ({s.type.__name__}, default {json.dumps(s.default)})")
                print(f"    {s.help}")
            return EXIT_OK
        if sub == "get" and len(rest) == 1:
            print(json.dumps(settings.get(rest[0])))
            return EXIT_OK
        if sub == "set" and len(rest) == 2:
            value = settings.set(rest[0], rest[1], actor=_actor())
            print(f"{rest[0]} = {json.dumps(value)}")
            return EXIT_OK
        if sub == "reset" and len(rest) == 1:
            settings.reset(rest[0], actor=_actor())
            print(f"{rest[0]} reset to {json.dumps(settings.get(rest[0]))}")
            return EXIT_OK
    except settings.SettingError as e:
        print(str(e), file=sys.stderr)
        return EXIT_USAGE
    except sqlite3.OperationalError as e:
        print(f"{e}: run `capex db migrate` first", file=sys.stderr)
        return EXIT_ERROR
    return _usage(SETTINGS_USAGE)
