"""HKEXnews fetcher.

Finds the latest filing matching (stock_code, form_type) from the
HKEXnews Electronic Disclosure System JSON feed, downloads the PDF,
and writes it atomically to `data/_sources/<TICKER>/_raw/`.

HKEXnews does not have a clean REST API. The search interface is a
JSF application that requires server-side session state and is
impractical to scrape via stdlib. Instead, this fetcher uses the
paginated JSON feed at:

    https://www1.hkexnews.hk/ncms/json/eds/lcisehk1relsdc_{page}.json

Each page contains ~500 recent filings across ALL SEHK-listed companies.
Pages 1-9 typically cover ~2 years of filings. The fetcher scans pages
sequentially, filtering for the target stock code and filing category,
and returns the most recent match.

Filing categories used:
    40100 = Annual Report     → maps to our form_type 'HK-AR'
    40200 = Interim Report    → maps to our form_type 'HK-IR'

HKEXnews serves filings as PDFs (sometimes with a `_c.pdf` suffix for
Chinese or `_e.pdf` for English). If only the Chinese version is
available, we use it — most HK annual reports are bilingual anyway.

What this module does NOT do:
- Write the sidecar JSON. Caller (dispatcher) does that.
- Touch the database.
- Apply canonical naming.
"""
from __future__ import annotations

import hashlib
import json
import re
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from . import FETCHER_VERSION, get_user_agent
from .errors import (
    FilingNotFoundError,
    IntegrityError,
    SourceUnavailableError,
    SuspiciousFilingSizeError,
)

REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCES_DIR = REPO_ROOT / "data" / "_sources"

FEED_URL = "https://www1.hkexnews.hk/ncms/json/eds/lcisehk1relsdc_{page}.json"
BASE_URL = "https://www1.hkexnews.hk"
MAX_FEED_PAGES = 9  # typically covers ~2 years

PROTOCOL_VERSION = "0.1.0-draft"

# Map our form_type tokens to HKEXnews tier-2 category codes
HKEX_FORM_TYPES = ("HK-AR", "HK-IR")
T2_CATEGORY = {
    "HK-AR": "40100",  # Annual Report
    "HK-IR": "40200",  # Interim/Half-Year Report
}


def fetch_latest(ticker: str, stock_code: str, form_type: str) -> dict[str, Any]:
    """Fetch the latest HK filing matching (stock_code, form_type).

    Args:
        ticker: companies.ticker key (e.g. "0700"). Used for the output
            folder name.
        stock_code: hkex_stock_code from companies table, zero-padded to
            5 digits (e.g. "00700").
        form_type: one of HKEX_FORM_TYPES.

    Returns:
        Metadata dict with all sidecar fields populated.

    Raises:
        FilingNotFoundError: no matching filing in the feed (may be older
            than ~2 years, or the company hasn't filed yet).
        SourceUnavailableError: HKEXnews returned non-2xx or unparseable.
        SuspiciousFilingSizeError: downloaded bytes outside 50KB-200MB.
    """
    if form_type not in HKEX_FORM_TYPES:
        raise ValueError(f"hkex fetcher does not support form_type={form_type!r}")

    t2_target = T2_CATEGORY[form_type]
    padded = stock_code.zfill(5)

    # Scan the JSON feed pages for the most recent matching filing.
    match = _scan_feed(padded, t2_target)
    if match is None:
        raise FilingNotFoundError(ticker, form_type)

    web_path = match["webPath"]
    pdf_url = BASE_URL + web_path

    # Try to find an English version if the match is Chinese.
    english_url = _try_english_variant(pdf_url)
    if english_url:
        pdf_url = english_url

    time.sleep(0.5)  # polite rate limiting for HKEXnews

    # Download the PDF.
    body = _http_get_bytes(pdf_url)
    size = len(body)
    if size < SuspiciousFilingSizeError.MIN_BYTES or size > SuspiciousFilingSizeError.MAX_BYTES:
        raise SuspiciousFilingSizeError(pdf_url, size)

    sha256 = hashlib.sha256(body).hexdigest()

    # Parse filing date from relTime (format: "DD/MM/YYYY HH:MM")
    filing_date = _parse_rel_time_to_date(match["relTime"])

    # Derive period_of_report from the filing title or from a simple heuristic.
    period_of_report = _derive_period_of_report(match, filing_date, form_type)

    # Sanitize filename and write to _raw/.
    raw_dir = SOURCES_DIR / ticker / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    original_filename = web_path.split("/")[-1]
    sanitized = _sanitize_filename(original_filename)
    raw_path = raw_dir / sanitized

    if raw_path.exists():
        existing_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        if existing_hash == sha256:
            return _build_metadata(
                ticker=ticker,
                raw_path=raw_path,
                sha256=sha256,
                source_url=pdf_url,
                form_type=form_type,
                filing_date=filing_date,
                period_of_report=period_of_report,
            )
        raw_path = raw_path.with_name(f"{raw_path.stem}-{sha256[:8]}{raw_path.suffix}")

    _atomic_write(raw_path, body)

    on_disk_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    if on_disk_hash != sha256:
        raw_path.unlink(missing_ok=True)
        raise IntegrityError(expected=sha256, actual=on_disk_hash)

    return _build_metadata(
        ticker=ticker,
        raw_path=raw_path,
        sha256=sha256,
        source_url=pdf_url,
        form_type=form_type,
        filing_date=filing_date,
        period_of_report=period_of_report,
    )


# ----------------------------------------------------------------------------
# Feed scanning
# ----------------------------------------------------------------------------


def _scan_feed(padded_stock_code: str, t2_target: str) -> dict | None:
    """Scan JSON feed pages for the most recent matching filing.

    Returns the first (most recent) item where:
    - stock[].sc matches padded_stock_code
    - t2Code contains t2_target
    """
    for page in range(1, MAX_FEED_PAGES + 1):
        url = FEED_URL.format(page=page)
        try:
            data = _http_get_json(url)
        except SourceUnavailableError:
            break  # no more pages

        for item in data.get("newsInfoLst", []):
            stocks = [s.get("sc", "") for s in item.get("stock", [])]
            t2_codes = item.get("t2Code", "")
            if padded_stock_code in stocks and t2_target in str(t2_codes):
                return item

        time.sleep(0.1)

    return None


# ----------------------------------------------------------------------------
# Helpers
# ----------------------------------------------------------------------------


def _try_english_variant(url: str) -> str | None:
    """If url ends with _c.pdf, check if an English version exists at _e.pdf."""
    if not url.endswith("_c.pdf"):
        return None
    english_url = url[:-6] + "_e.pdf"
    try:
        req = urllib.request.Request(english_url, method="HEAD", headers={"User-Agent": get_user_agent()})
        resp = urllib.request.urlopen(req, timeout=10)
        if resp.status == 200:
            return english_url
    except (urllib.error.HTTPError, urllib.error.URLError):
        pass
    return None


def _parse_rel_time_to_date(rel_time: str) -> str:
    """Parse "DD/MM/YYYY HH:MM" → "YYYY-MM-DD"."""
    parts = rel_time.strip().split(" ")[0].split("/")
    return f"{parts[2]}-{parts[1]}-{parts[0]}"


def _derive_period_of_report(
    item: dict, filing_date: str, form_type: str
) -> str:
    """Best-effort derivation of period_of_report from the filing.

    HKEXnews items don't have a structured period-of-report field like
    SEC EDGAR. We derive it from the title + filing date:
    - Annual reports: title usually contains the year (e.g. "2025 年報").
      Period = that year's December 31 (for Dec-FYE companies).
    - Interim reports: title usually contains a year. Period = June 30.

    If we can't parse the year from the title, fall back to filing_date year
    minus one (annual reports are typically filed 3-4 months after FYE).
    """
    title = item.get("title", "") + " " + item.get("lTxt", "")
    year_match = re.search(r"20[12]\d", title)

    if year_match:
        report_year = int(year_match.group(0))
    else:
        # Fallback: filing year - 1 for annuals (filed after FYE)
        filing_year = int(filing_date.split("-")[0])
        report_year = filing_year - 1 if form_type == "HK-AR" else filing_year

    if form_type == "HK-AR":
        return f"{report_year}-12-31"
    else:  # HK-IR
        return f"{report_year}-06-30"


def _build_metadata(
    *,
    ticker: str,
    raw_path: Path,
    sha256: str,
    source_url: str,
    form_type: str,
    filing_date: str,
    period_of_report: str,
) -> dict[str, Any]:
    return {
        "raw_path": str(raw_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "sha256": sha256,
        "source": "hkex",
        "source_url": source_url,
        "accession_number": None,
        "form_type": form_type,
        "filing_date": filing_date,
        "period_of_report": period_of_report,
        "ticker": ticker,
        "fetched_at": _now_iso(),
        "fetcher_version": FETCHER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
    }


def _http_get_json(url: str) -> dict:
    raw = _http_get_bytes(url)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SourceUnavailableError("hkex", None, f"unparseable JSON from {url}: {e}") from e


def _http_get_bytes(url: str) -> bytes:
    req = urllib.request.Request(url, headers={"User-Agent": get_user_agent()})
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            return resp.read()
    except urllib.error.HTTPError as e:
        raise SourceUnavailableError("hkex", e.code, f"GET {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise SourceUnavailableError("hkex", None, f"GET {url}: {e.reason}") from e


def _sanitize_filename(name: str) -> str:
    name = name.lower()
    name = re.sub(r"[^a-z0-9._-]", "-", name)
    name = re.sub(r"-+", "-", name)
    return name.strip("-")


def _atomic_write(path: Path, content: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
