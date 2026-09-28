"""One HTTP client for every SEC EDGAR request.

SEC's fair-access policy asks every client to identify itself (a contact
User-Agent) and to stay under 10 requests per second. This client:

- sends the project User-Agent (CAPEX_FETCHER_UA) and accepts gzip;
- spaces requests at least MIN_INTERVAL_S apart, process-wide (<= 5/s);
- retries 429 and 5xx responses and network errors with exponential
  backoff, honouring Retry-After;
- raises SourceUnavailableError when it gives up, so callers can tell
  "SEC is unreachable" apart from "no filing yet".
"""
from __future__ import annotations

import gzip
import json
import threading
import time
import urllib.error
import urllib.request
import zlib

from . import get_user_agent
from .errors import SourceUnavailableError

MIN_INTERVAL_S = 0.2        # <= 5 requests/second
MAX_ATTEMPTS = 4
BACKOFF_BASE_S = 2.0
MAX_RETRY_AFTER_S = 60.0
RETRY_STATUSES = frozenset({429, 500, 502, 503, 504})

_lock = threading.Lock()
_last_request_at = 0.0


def _throttle() -> None:
    global _last_request_at
    with _lock:
        wait = _last_request_at + MIN_INTERVAL_S - time.monotonic()
        if wait > 0:
            time.sleep(wait)
        _last_request_at = time.monotonic()


def _retry_after(err: urllib.error.HTTPError) -> float | None:
    value = err.headers.get("Retry-After") if err.headers else None
    try:
        return min(float(value), MAX_RETRY_AFTER_S) if value else None
    except ValueError:
        return None


def _decode(data: bytes, encoding: str) -> bytes:
    encoding = encoding.lower()
    if encoding == "gzip":
        return gzip.decompress(data)
    if encoding == "deflate":
        return zlib.decompress(data)
    return data


def get_bytes(url: str, *, timeout: float = 30) -> bytes:
    """GET `url` from SEC with throttling and retries."""
    headers = {"User-Agent": get_user_agent(), "Accept-Encoding": "gzip, deflate"}
    last_problem = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        _throttle()
        request = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(request, timeout=timeout) as resp:
                return _decode(resp.read(), resp.headers.get("Content-Encoding", ""))
        except urllib.error.HTTPError as e:
            if e.code not in RETRY_STATUSES:
                raise SourceUnavailableError("sec_edgar", e.code, f"GET {url}: {e.reason}") from e
            last_problem = f"HTTP {e.code}"
            delay = _retry_after(e)
        except (urllib.error.URLError, TimeoutError, ConnectionError) as e:
            last_problem = f"{type(e).__name__}: {getattr(e, 'reason', e)}"
            delay = None
        if attempt < MAX_ATTEMPTS:
            time.sleep(delay if delay is not None else BACKOFF_BASE_S ** attempt)
    raise SourceUnavailableError(
        "sec_edgar", None, f"GET {url}: gave up after {MAX_ATTEMPTS} attempts ({last_problem})"
    )


def get_json(url: str, *, timeout: float = 30) -> dict:
    """GET and parse a JSON document from SEC."""
    raw = get_bytes(url, timeout=timeout)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SourceUnavailableError("sec_edgar", None, f"unparseable JSON from {url}: {e}") from e
