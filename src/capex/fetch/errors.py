"""Shared error vocabulary for the fetch layer.

Every fetcher (SEC, HKEX) raises these. Callers (dispatcher, CLI,
skills) catch them and decide how to surface. Errors are exceptions —
no return-code style, no error tuples.
"""
from __future__ import annotations


class FetchError(Exception):
    """Base class for all fetch-layer errors. Catch this to handle any."""


class UnknownCompanyError(FetchError):
    """Ticker is not a key in the companies table."""

    def __init__(self, ticker: str) -> None:
        super().__init__(f"unknown ticker: {ticker!r} (run `capex db sync-companies`?)")
        self.ticker = ticker


class FormTypeMismatchError(FetchError):
    """Requested form_type is incompatible with the company's preferred source.

    Example: asking for 10-K on an HK-only company.
    """

    def __init__(self, ticker: str, requested: str, supported: tuple[str, ...]) -> None:
        super().__init__(
            f"{ticker}: form_type {requested!r} not supported by this source "
            f"(supported: {supported})"
        )
        self.ticker = ticker
        self.requested = requested
        self.supported = supported


class FilingNotFoundError(FetchError):
    """No filing matching (ticker, form_type) exists at the regulator yet."""

    def __init__(self, ticker: str, form_type: str) -> None:
        super().__init__(f"{ticker}: no {form_type} found at the regulator")
        self.ticker = ticker
        self.form_type = form_type


class SourceUnavailableError(FetchError):
    """Regulator returned a non-2xx status or unparseable response."""

    def __init__(self, source: str, http_status: int | None, message: str) -> None:
        suffix = f" (HTTP {http_status})" if http_status else ""
        super().__init__(f"{source}: {message}{suffix}")
        self.source = source
        self.http_status = http_status


class SuspiciousFilingSizeError(FetchError):
    """File size is outside sane bounds (50 KB to 200 MB).

    Catches truncated downloads and runaway responses. The bounds are
    intentionally loose — they're not meant to enforce real limits, just
    to surface obvious anomalies.
    """

    MIN_BYTES = 50 * 1024
    MAX_BYTES = 200 * 1024 * 1024

    def __init__(self, path: str, size_bytes: int) -> None:
        super().__init__(
            f"suspicious size for {path}: {size_bytes:,} bytes "
            f"(expected {self.MIN_BYTES:,} – {self.MAX_BYTES:,})"
        )
        self.path = path
        self.size_bytes = size_bytes


class IntegrityError(FetchError):
    """Hash of the file on disk doesn't match what was downloaded."""

    def __init__(self, expected: str, actual: str) -> None:
        super().__init__(f"hash mismatch: expected {expected}, got {actual}")
        self.expected = expected
        self.actual = actual
