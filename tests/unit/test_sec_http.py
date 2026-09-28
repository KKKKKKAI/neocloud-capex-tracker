"""The shared SEC HTTP client: throttling, retries, errors. No network."""
from __future__ import annotations

import email.message
import gzip
import io
import urllib.error

import pytest

from capex.fetch import sec_http
from capex.fetch.errors import SourceUnavailableError
from capex.fetch.sec import list_filings


class _Resp:
    def __init__(self, body: bytes, encoding: str = ""):
        self._body = body
        self.headers = email.message.Message()
        if encoding:
            self.headers["Content-Encoding"] = encoding

    def read(self):
        return self._body

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def _http_error(code: int, retry_after: str | None = None):
    headers = email.message.Message()
    if retry_after:
        headers["Retry-After"] = retry_after
    return urllib.error.HTTPError("https://sec.test", code, "err", headers, io.BytesIO(b""))


@pytest.fixture
def fake_net(monkeypatch):
    """Script urlopen responses; record sleeps and request headers."""
    script: list = []
    seen: dict = {"sleeps": [], "headers": []}

    def urlopen(request, timeout):
        seen["headers"].append(dict(request.header_items()))
        item = script.pop(0)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(sec_http.urllib.request, "urlopen", urlopen)
    monkeypatch.setattr(sec_http.time, "sleep", seen["sleeps"].append)
    monkeypatch.setattr(sec_http, "MIN_INTERVAL_S", 0.0)
    monkeypatch.setenv("CAPEX_FETCHER_UA", "tests contact@example.com")
    return script, seen


def test_success_sends_contact_user_agent_and_gzip(fake_net):
    script, seen = fake_net
    script.append(_Resp(gzip.compress(b'{"ok": true}'), "gzip"))
    assert sec_http.get_json("https://sec.test/x.json") == {"ok": True}
    headers = seen["headers"][0]
    assert headers["User-agent"] == "tests contact@example.com"
    assert "gzip" in headers["Accept-encoding"]


def test_429_honours_retry_after(fake_net):
    script, seen = fake_net
    script += [_http_error(429, "3"), _Resp(b"ok")]
    assert sec_http.get_bytes("https://sec.test/x") == b"ok"
    assert seen["sleeps"] == [3.0]


def test_5xx_and_network_errors_back_off_then_succeed(fake_net):
    script, seen = fake_net
    script += [_http_error(503), urllib.error.URLError("reset"), _Resp(b"ok")]
    assert sec_http.get_bytes("https://sec.test/x") == b"ok"
    assert seen["sleeps"] == [2.0, 4.0]


def test_404_is_not_retried(fake_net):
    script, seen = fake_net
    script.append(_http_error(404))
    with pytest.raises(SourceUnavailableError) as info:
        sec_http.get_bytes("https://sec.test/missing")
    assert info.value.http_status == 404
    assert seen["sleeps"] == []


def test_gives_up_after_max_attempts(fake_net):
    script, _ = fake_net
    script += [_http_error(500)] * sec_http.MAX_ATTEMPTS
    with pytest.raises(SourceUnavailableError, match="gave up"):
        sec_http.get_bytes("https://sec.test/x")


def test_retry_after_is_capped(fake_net):
    script, seen = fake_net
    script += [_http_error(429, "86400"), _Resp(b"ok")]
    sec_http.get_bytes("https://sec.test/x")
    assert seen["sleeps"] == [sec_http.MAX_RETRY_AFTER_S]


def _submissions(*rows):
    cols = ("accessionNumber", "filingDate", "reportDate", "primaryDocument", "form")
    return {"filings": {"recent": {c: [r[i] for r in rows] for i, c in enumerate(cols)}}}


def test_list_filings_newest_first_without_amendments():
    subs = _submissions(
        ("a3", "2026-08-01", "2026-06-30", "q3.htm", "10-Q/A"),
        ("a2", "2026-07-30", "2026-06-30", "q2.htm", "10-Q"),
        ("a1", "2026-04-30", "2026-03-31", "q1.htm", "10-Q"),
        ("k1", "2026-02-01", "2025-12-31", "k.htm", "10-K"),
    )
    assert [f["accessionNumber"] for f in list_filings(subs, "10-Q")] == ["a2", "a1"]
    with_amendments = list_filings(subs, "10-Q", include_amendments=True)
    assert [f["accessionNumber"] for f in with_amendments] == ["a3", "a2", "a1"]
