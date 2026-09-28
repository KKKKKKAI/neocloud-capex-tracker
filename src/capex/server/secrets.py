"""Load runtime secrets from AWS SSM Parameter Store into an env file.

Runs on the server as the `capex-secrets` systemd oneshot, before the
services that need the values. Every parameter directly under
CAPEX_SECRETS_PATH (default `/capex/`) becomes one `NAME='value'` line
in a root-owned, group-readable file on tmpfs (`/run/capex/capex.env`),
so secrets never touch the data volume, git, or logs.

    python -m capex.server.secrets fetch [--path /capex/] [--out FILE] [--group capex]
    python -m capex.server.secrets check [--path /capex/]   # names and types only

Nothing in this module ever prints or logs a value.
"""
from __future__ import annotations

import argparse
import os
import re
import sys
from pathlib import Path

DEFAULT_PATH = "/capex/"
DEFAULT_OUT = Path("/run/capex/capex.env")
REQUIRED = ("CLAUDE_CODE_OAUTH_TOKEN", "ALPHA_VANTAGE_API_KEY")
OPTIONAL = ("GMAIL_USERNAME", "GMAIL_APP_PASSWORD")
# Parameters that are not sensitive and may be stored as plain String.
NOT_SECRET = frozenset({"GMAIL_USERNAME"})

_ENV_NAME = re.compile(r"^[A-Z][A-Z0-9_]*$")


class SecretsError(RuntimeError):
    """A parameter can't be written to the env file safely."""


def _ssm_client(region: str | None = None):
    import boto3

    return boto3.client("ssm", region_name=region)


def fetch_parameters(client, path: str = DEFAULT_PATH) -> dict[str, str]:
    """`{ENV_NAME: value}` for every parameter directly under `path`."""
    values: dict[str, str] = {}
    paginator = client.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=path, Recursive=False, WithDecryption=True):
        for param in page["Parameters"]:
            name = param["Name"].rsplit("/", 1)[-1]
            if not _ENV_NAME.match(name):
                print(f"skipping {param['Name']}: not an env var name", file=sys.stderr)
                continue
            values[name] = param["Value"]
    return values


def render_env(values: dict[str, str]) -> str:
    """Single-quoted `NAME='value'` lines.

    Single quotes mean the same thing to systemd's EnvironmentFile= and
    to a shell `source`: no escapes, no `$` expansion. A value that
    contains a quote or a line break is rejected rather than mangled.
    """
    lines = []
    for name in sorted(values):
        value = values[name]
        if "'" in value or "\n" in value or "\r" in value:
            raise SecretsError(f"{name}: value contains a quote or line break")
        lines.append(f"{name}='{value}'")
    return "\n".join(lines) + "\n"


def write_env_file(values: dict[str, str], out: Path, group: str | None = None) -> None:
    """Atomically write `values` to `out` with mode 0640 (group-readable)."""
    body = render_env(values)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(body)
        if group:
            import grp

            os.chown(tmp, -1, grp.getgrnam(group).gr_gid)
        os.chmod(tmp, 0o640)
        os.replace(tmp, out)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise


def describe(client, path: str = DEFAULT_PATH) -> dict[str, str]:
    """`{ENV_NAME: parameter type}` without reading any value."""
    types: dict[str, str] = {}
    paginator = client.get_paginator("describe_parameters")
    filters = [{"Key": "Name", "Option": "BeginsWith", "Values": [path]}]
    for page in paginator.paginate(ParameterFilters=filters):
        for param in page["Parameters"]:
            rest = param["Name"][len(path):]
            if "/" not in rest:
                types[rest] = param["Type"]
    return types


def problems(types: dict[str, str]) -> list[str]:
    """Human-readable issues with the parameter set (names only)."""
    issues = [f"missing required parameter {n}" for n in REQUIRED if n not in types]
    issues += [f"optional parameter {n} is not set" for n in OPTIONAL if n not in types]
    issues += [
        f"{n} is a {t}; store it as SecureString"
        for n, t in sorted(types.items())
        if t != "SecureString" and n not in NOT_SECRET
    ]
    return issues


def _cmd_check(client, path: str) -> int:
    types = describe(client, path)
    for name in sorted(types):
        print(f"{path}{name}  {types[name]}")
    issues = problems(types)
    for issue in issues:
        print(f"  ! {issue}")
    missing_required = any(i.startswith("missing required") for i in issues)
    return 1 if missing_required else 0


def _cmd_fetch(client, path: str, out: Path, group: str | None) -> int:
    values = fetch_parameters(client, path)
    missing = [n for n in REQUIRED if n not in values]
    if missing:
        print(f"missing required parameters under {path}: {', '.join(missing)}",
              file=sys.stderr)
        return 1
    for name in OPTIONAL:
        if name not in values:
            print(f"optional parameter {path}{name} is not set", file=sys.stderr)
    try:
        write_env_file(values, out, group)
    except SecretsError as e:
        print(str(e), file=sys.stderr)
        return 1
    print(f"wrote {len(values)} values to {out}: {', '.join(sorted(values))}")
    return 0


def main(argv: list[str] | None = None, client=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m capex.server.secrets")
    sub = parser.add_subparsers(dest="cmd", required=True)
    for name in ("fetch", "check"):
        p = sub.add_parser(name)
        p.add_argument("--path", default=os.environ.get("CAPEX_SECRETS_PATH", DEFAULT_PATH))
        p.add_argument("--region", default=None)
        if name == "fetch":
            p.add_argument("--out", type=Path, default=DEFAULT_OUT)
            p.add_argument("--group", default=None)
    args = parser.parse_args(argv)

    path = args.path if args.path.endswith("/") else args.path + "/"
    client = client or _ssm_client(args.region)
    if args.cmd == "check":
        return _cmd_check(client, path)
    return _cmd_fetch(client, path, args.out, args.group)


if __name__ == "__main__":
    sys.exit(main())
