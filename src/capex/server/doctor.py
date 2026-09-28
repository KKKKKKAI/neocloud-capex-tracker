"""Health checks for the server — also the Phase 2 smoke test.

    python -m capex.server.doctor                  # every check
    python -m capex.server.doctor --only claude,sec
    python -m capex.server.doctor --placeholder    # also seed index.html in the site bucket

Checks read their configuration from the environment the services run
with (/etc/capex/capex.conf plus /run/capex/capex.env) and report PASS,
FAIL or SKIP with a one-line detail. No check ever prints a secret.
Exit status is 1 when any check fails.
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import smtplib
import sys
import urllib.error
import urllib.parse
import urllib.request
import uuid
from collections.abc import Callable
from dataclasses import asdict, dataclass

PASS, FAIL, SKIP = "PASS", "FAIL", "SKIP"

SEC_PROBE_URL = "https://data.sec.gov/submissions/CIK0000789019.json"  # MSFT
ALPHA_VANTAGE_URL = "https://www.alphavantage.co/query"
GMAIL_SMTP = ("smtp.gmail.com", 465)
CLAUDE_TIMEOUT_S = 180

PLACEHOLDER_HTML = (
    "<!doctype html><html lang=\"en\"><head><meta charset=\"utf-8\">"
    "<title>neocloud capex tracker</title></head><body>"
    "<p>The tracker is being set up. The dashboard appears here after the "
    "first publish.</p></body></html>\n"
)

@dataclass
class Result:
    name: str
    status: str
    detail: str


def _http_get(url: str, headers: dict[str, str] | None = None,
              timeout: int = 20) -> tuple[int, bytes]:
    request = urllib.request.Request(url, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()


def check_claude() -> Result:
    """One real call through the production backend and settings (model,
    budget, pause). It is logged in llm_calls like any other call."""
    from ..adapters.cli_backend import CLIBackend, claude_binary
    from ..adapters.errors import LLMError

    if not claude_binary():
        return Result("claude", FAIL, "claude CLI not found (set CAPEX_CLAUDE_BIN)")
    backend = CLIBackend.from_settings(timeout=CLAUDE_TIMEOUT_S)
    try:
        answer = backend.extract("", "Reply with the single word OK.")
    except LLMError as e:
        return Result("claude", FAIL, f"{type(e).__name__}: {e}")
    if "OK" not in answer.upper():
        return Result("claude", FAIL, f"unexpected answer: {answer[:80]!r}")
    ms = (backend.last_call or {}).get("duration_ms", "?")
    return Result("claude", PASS, f"{backend.model} answered in {ms} ms")


def check_sec() -> Result:
    from ..fetch import get_user_agent

    try:
        status, _ = _http_get(SEC_PROBE_URL, {"User-Agent": get_user_agent()})
    except (urllib.error.URLError, OSError) as e:
        return Result("sec", FAIL, f"network error: {type(e).__name__}")
    ok = status == 200
    hint = "" if ok else " (SEC rejects requests without a contact User-Agent)"
    return Result("sec", PASS if ok else FAIL, f"HTTP {status} for MSFT submissions{hint}")


def check_alpha_vantage() -> Result:
    key = os.environ.get("ALPHA_VANTAGE_API_KEY")
    if not key or key == "demo":
        return Result("alpha_vantage", FAIL, "ALPHA_VANTAGE_API_KEY not set")
    query = urllib.parse.urlencode(
        {"function": "EARNINGS_CALENDAR", "horizon": "3month", "apikey": key}
    )
    try:
        status, body = _http_get(f"{ALPHA_VANTAGE_URL}?{query}")
    except (urllib.error.URLError, OSError) as e:
        # Never echo the URL: it carries the key.
        return Result("alpha_vantage", FAIL, f"network error: {type(e).__name__}")
    text = body.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    if status == 200 and lines and lines[0].lower().startswith("symbol,"):
        return Result("alpha_vantage", PASS, f"{len(lines) - 1} upcoming earnings rows")
    try:
        message = str(next(iter(json.loads(text).values())))
    except (ValueError, StopIteration, AttributeError):
        message = lines[0] if lines else "empty response"
    return Result("alpha_vantage", FAIL, f"HTTP {status}: {message.replace(key, '***')[:160]}")


def check_gmail() -> Result:
    user = os.environ.get("GMAIL_USERNAME")
    password = os.environ.get("GMAIL_APP_PASSWORD")
    if not user or not password:
        return Result("gmail", FAIL, "GMAIL_USERNAME / GMAIL_APP_PASSWORD not set")
    try:
        with smtplib.SMTP_SSL(*GMAIL_SMTP, timeout=20) as smtp:
            # Google displays app passwords in groups of four; SMTP wants none.
            smtp.login(user, password.replace(" ", ""))
    except smtplib.SMTPAuthenticationError:
        return Result("gmail", FAIL,
                      "login rejected: check the app password (needs 2-Step Verification)")
    except (OSError, smtplib.SMTPException) as e:
        return Result("gmail", FAIL, f"{type(e).__name__}")
    return Result("gmail", PASS, f"SMTP login OK as {user}")


def _site_config() -> tuple[str, str] | None:
    bucket = os.environ.get("CAPEX_SITE_BUCKET")
    base = os.environ.get("CAPEX_PUBLIC_BASE_URL")
    return (bucket, base.rstrip("/")) if bucket and base else None


def check_publish() -> Result:
    config = _site_config()
    if config is None:
        return Result("publish", SKIP, "CAPEX_SITE_BUCKET / CAPEX_PUBLIC_BASE_URL not set")
    bucket, base = config
    import boto3

    s3 = boto3.client("s3")
    key = f"_doctor/{uuid.uuid4().hex}.txt"
    s3.put_object(Bucket=bucket, Key=key, Body=key.encode(), ContentType="text/plain",
                  CacheControl="no-store")
    try:
        status, body = _http_get(f"{base}/{key}")
    finally:
        s3.delete_object(Bucket=bucket, Key=key)
    ok = status == 200 and body == key.encode()
    return Result("publish", PASS if ok else FAIL, f"S3 -> CloudFront round trip: HTTP {status}")


def ensure_placeholder() -> Result:
    """Seed index.html so the public URL answers before the first publish."""
    config = _site_config()
    if config is None:
        return Result("placeholder", SKIP, "CAPEX_SITE_BUCKET / CAPEX_PUBLIC_BASE_URL not set")
    bucket, base = config
    import boto3
    from botocore.exceptions import ClientError

    s3 = boto3.client("s3")
    try:
        s3.head_object(Bucket=bucket, Key="index.html")
        return Result("placeholder", SKIP, "index.html already present")
    except ClientError as e:
        if e.response.get("Error", {}).get("Code") not in ("404", "NoSuchKey", "NotFound"):
            raise
    s3.put_object(Bucket=bucket, Key="index.html", Body=PLACEHOLDER_HTML.encode(),
                  ContentType="text/html; charset=utf-8", CacheControl="max-age=60")
    return Result("placeholder", PASS, f"uploaded placeholder: {base}/")


def check_memory() -> Result:
    try:
        with open("/proc/meminfo", encoding="ascii") as f:
            info = {k: int(v.split()[0]) for k, v in (ln.split(":", 1) for ln in f)}
    except OSError:
        return Result("memory", SKIP, "/proc/meminfo not available")
    mem, swap = info.get("MemTotal", 0) // 1024, info.get("SwapTotal", 0) // 1024
    avail = info.get("MemAvailable", 0) // 1024
    ok = mem + swap >= 2500
    return Result("memory", PASS if ok else FAIL,
                  f"RAM {mem} MiB ({avail} MiB available) + swap {swap} MiB")


def check_disk() -> Result:
    home = os.environ.get("CAPEX_HOME") or "."
    usage = shutil.disk_usage(home)
    free_gib = usage.free / 2**30
    return Result("disk", PASS if free_gib >= 1 else FAIL,
                  f"{free_gib:.1f} GiB free of {usage.total / 2**30:.1f} GiB at {home}")


def check_data_mount() -> Result:
    home = os.environ.get("CAPEX_HOME")
    if not home or not os.environ.get("CAPEX_DATA_VOLUME_ID"):
        return Result("data_mount", SKIP, "not a server (CAPEX_DATA_VOLUME_ID unset)")
    ok = os.path.ismount(home)
    return Result("data_mount", PASS if ok else FAIL,
                  f"{home} is {'' if ok else 'NOT '}a mount point")


CHECKS: dict[str, Callable[[], Result]] = {
    "claude": check_claude,
    "sec": check_sec,
    "alpha_vantage": check_alpha_vantage,
    "gmail": check_gmail,
    "publish": check_publish,
    "memory": check_memory,
    "disk": check_disk,
    "data_mount": check_data_mount,
}


def run_checks(names: list[str], placeholder: bool = False) -> list[Result]:
    tasks = ([("placeholder", ensure_placeholder)] if placeholder else [])
    tasks += [(n, CHECKS[n]) for n in names]
    results = []
    for name, fn in tasks:
        try:
            results.append(fn())
        except Exception as e:  # a crashing check is a failed check
            results.append(Result(name, FAIL, f"{type(e).__name__}: {e}"))
    return results


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m capex.server.doctor")
    parser.add_argument("--only", help=f"comma-separated subset of: {', '.join(CHECKS)}")
    parser.add_argument("--placeholder", action="store_true",
                        help="upload a placeholder index.html if the site bucket has none")
    parser.add_argument("--json", action="store_true", help="machine-readable output")
    args = parser.parse_args(argv)

    names = [n.strip() for n in args.only.split(",")] if args.only else list(CHECKS)
    unknown = [n for n in names if n not in CHECKS]
    if unknown:
        parser.error(f"unknown checks: {', '.join(unknown)}")

    results = run_checks(names, placeholder=args.placeholder)
    if args.json:
        print(json.dumps([asdict(r) for r in results], indent=2))
    else:
        width = max(len(r.name) for r in results)
        for r in results:
            print(f"{r.status:4}  {r.name:<{width}}  {r.detail}")
    return 1 if any(r.status == FAIL for r in results) else 0


if __name__ == "__main__":
    sys.exit(main())
