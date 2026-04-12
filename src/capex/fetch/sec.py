"""SEC EDGAR fetcher.

Hits SEC's free submissions API at `data.sec.gov/submissions/CIK*.json`,
finds the latest filing matching a requested form_type, downloads the
primary document bytes, writes them atomically to
`data/_sources/<TICKER>/_raw/`, and returns a metadata dict that the
dispatcher uses to write the sidecar JSON and the source_documents row.

Stdlib only — no `requests`, no `httpx`. urllib.request is enough.

Rate limiting: SEC's published limit is 10 req/sec. Each invocation
makes 2 HTTP calls (submissions API + the primary document), so we sleep
0.1s between them. In practice this is overkill for one filing per call.

User-Agent: read from CAPEX_FETCHER_UA env var (see `__init__.py`). SEC
rejects/throttles requests without a real contact email.

What this module does NOT do:
- Write the sidecar JSON. Caller (dispatcher) does that.
- Touch the database. Caller (dispatcher) does that.
- Apply canonical naming. Files in _raw/ keep their regulator-served name.
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

# Resolve repo root from this file's location: src/capex/fetch/sec.py → ../../../
REPO_ROOT = Path(__file__).resolve().parents[3]
SOURCES_DIR = REPO_ROOT / "data" / "_sources"

SUBMISSIONS_URL = "https://data.sec.gov/submissions/CIK{cik_padded}.json"
ARCHIVE_URL = "https://www.sec.gov/Archives/edgar/data/{cik_int}/{accession_no_dashes}/{filename}"

REQUEST_INTERVAL_SECONDS = 0.1  # SEC limit is 10 req/sec; we use far less

PROTOCOL_VERSION = "0.1.0-draft"

# SEC form types this fetcher knows how to ask for. The fetcher passes
# the form_type through to the API filter, so adding new types is just
# extending this set (no parser changes needed).
SEC_FORM_TYPES = ("10-K", "10-Q", "20-F")


def fetch_latest(ticker: str, cik: str, form_type: str) -> dict[str, Any]:
    """Fetch the latest filing of the requested form_type from SEC EDGAR.

    Args:
        ticker: companies.ticker key (e.g. "MSFT"). Used only for the
            output folder name; the actual SEC lookup uses cik.
        cik: SEC CIK as a zero-padded 10-digit string from companies.edgar_cik
            (e.g. "0000789019").
        form_type: one of SEC_FORM_TYPES.

    Returns:
        Metadata dict with all sidecar fields populated. Caller is
        responsible for writing the sidecar JSON and inserting the
        source_documents row.

    Raises:
        FilingNotFoundError: no matching form in the recent filings.
        SourceUnavailableError: SEC returned non-2xx or unparseable response.
        SuspiciousFilingSizeError: downloaded bytes outside 50KB-200MB.
        IntegrityError: hash recomputation mismatch (very unlikely).
    """
    if form_type not in SEC_FORM_TYPES:
        # Caller should have caught this in the dispatcher; defensive check.
        raise ValueError(f"sec fetcher does not support form_type={form_type!r}")

    cik_padded = cik.lstrip("0").zfill(10)
    cik_int = str(int(cik))  # no leading zeros for the archive URL

    # 1. Get the company's recent filings index.
    submissions = _http_get_json(SUBMISSIONS_URL.format(cik_padded=cik_padded))

    # 2. Find the most recent matching filing.
    filing = _find_latest(submissions, form_type)
    if filing is None:
        raise FilingNotFoundError(ticker, form_type)

    accession = filing["accessionNumber"]
    accession_no_dashes = accession.replace("-", "")
    primary_doc = filing["primaryDocument"]
    filing_date = filing["filingDate"]
    period_of_report = filing["reportDate"]

    source_url = ARCHIVE_URL.format(
        cik_int=cik_int,
        accession_no_dashes=accession_no_dashes,
        filename=primary_doc,
    )

    time.sleep(REQUEST_INTERVAL_SECONDS)

    # 3. Download the primary document bytes.
    body = _http_get_bytes(source_url)

    # 4. Sanity-check size.
    size = len(body)
    if size < SuspiciousFilingSizeError.MIN_BYTES or size > SuspiciousFilingSizeError.MAX_BYTES:
        raise SuspiciousFilingSizeError(source_url, size)

    # 5. Compute hash and write to _raw/ with canonical name.
    sha256 = hashlib.sha256(body).hexdigest()

    raw_dir = SOURCES_DIR / ticker / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)

    # Build canonical filename at download time — no separate organize step.
    # Format: [dd.mm.yyyy][TICKER][PERIOD][FORM].<ext>
    ext = Path(primary_doc).suffix or ".htm"
    from ..organize.namer import canonical_name, compute_period_token
    # Look up FYE month from companies table (fall back to 12 if not found)
    _fye = _get_fye_month(ticker)
    _period_token = compute_period_token(form_type, period_of_report, _fye, ticker=ticker)
    canon = canonical_name(filing_date, ticker, _period_token, form_type, ext)
    raw_path = raw_dir / canon

    # Dedup: if this exact file already exists, return existing.
    if raw_path.exists():
        existing_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
        if existing_hash == sha256:
            return _build_metadata(
                ticker=ticker,
                raw_path=raw_path,
                sha256=sha256,
                source_url=source_url,
                accession=accession,
                form_type=form_type,
                filing_date=filing_date,
                period_of_report=period_of_report,
            )
        # Hash differs — append short hash to disambiguate.
        stem, suffix = raw_path.stem, raw_path.suffix
        raw_path = raw_path.with_name(f"{stem}-{sha256[:8]}{suffix}")

    _atomic_write(raw_path, body)

    # 6. Verify the bytes on disk match what we just hashed.
    on_disk_hash = hashlib.sha256(raw_path.read_bytes()).hexdigest()
    if on_disk_hash != sha256:
        raw_path.unlink(missing_ok=True)
        raise IntegrityError(expected=sha256, actual=on_disk_hash)

    return _build_metadata(
        ticker=ticker,
        raw_path=raw_path,
        sha256=sha256,
        source_url=source_url,
        accession=accession,
        form_type=form_type,
        filing_date=filing_date,
        period_of_report=period_of_report,
    )


# ----------------------------------------------------------------------------
# Internal helpers
# ----------------------------------------------------------------------------


def _find_latest(submissions: dict, form_type: str) -> dict | None:
    """Pick the most recent filing matching form_type from the submissions JSON.

    Returns a dict with keys: accessionNumber, filingDate, reportDate,
    primaryDocument. Returns None if no match.

    Only looks at filings.recent (typically the most recent ~1000 filings,
    plenty for finding the latest annual or quarterly).
    """
    recent = submissions.get("filings", {}).get("recent", {})
    forms = recent.get("form", [])
    accessions = recent.get("accessionNumber", [])
    filing_dates = recent.get("filingDate", [])
    report_dates = recent.get("reportDate", [])
    primary_docs = recent.get("primaryDocument", [])

    for i, form in enumerate(forms):
        if form == form_type:
            return {
                "accessionNumber": accessions[i],
                "filingDate": filing_dates[i],
                "reportDate": report_dates[i],
                "primaryDocument": primary_docs[i],
            }
    return None


def _build_metadata(
    *,
    ticker: str,
    raw_path: Path,
    sha256: str,
    source_url: str,
    accession: str,
    form_type: str,
    filing_date: str,
    period_of_report: str,
) -> dict[str, Any]:
    """Assemble the metadata dict that becomes the sidecar JSON + DB row."""
    return {
        "raw_path": str(raw_path.relative_to(REPO_ROOT)).replace("\\", "/"),
        "sha256": sha256,
        "source": "sec_edgar",
        "source_url": source_url,
        "accession_number": accession,
        "form_type": form_type,
        "filing_date": filing_date,
        "period_of_report": period_of_report,
        "ticker": ticker,
        "fetched_at": _now_iso(),
        "fetcher_version": FETCHER_VERSION,
        "protocol_version": PROTOCOL_VERSION,
    }


def _http_get_json(url: str) -> dict:
    """GET a JSON resource with the project User-Agent. Raises SourceUnavailableError on failure."""
    raw = _http_get_bytes(url)
    try:
        return json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as e:
        raise SourceUnavailableError("sec_edgar", None, f"unparseable JSON from {url}: {e}") from e


def _http_get_bytes(url: str) -> bytes:
    """GET raw bytes with the project User-Agent. Raises SourceUnavailableError on failure."""
    headers = {"User-Agent": get_user_agent(), "Accept-Encoding": "gzip, deflate"}
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            data = resp.read()
            # urllib doesn't auto-decompress; do it manually if needed.
            encoding = resp.headers.get("Content-Encoding", "").lower()
            if encoding == "gzip":
                import gzip
                data = gzip.decompress(data)
            elif encoding == "deflate":
                import zlib
                data = zlib.decompress(data)
            return data
    except urllib.error.HTTPError as e:
        raise SourceUnavailableError("sec_edgar", e.code, f"GET {url}: {e.reason}") from e
    except urllib.error.URLError as e:
        raise SourceUnavailableError("sec_edgar", None, f"GET {url}: {e.reason}") from e


def _get_fye_month(ticker: str) -> int:
    """Look up fiscal year end month from the DB, default 12."""
    import sqlite3
    db_path = REPO_ROOT / "data" / "db" / "capex.db"
    if not db_path.exists():
        return 12
    conn = sqlite3.connect(str(db_path))
    row = conn.execute(
        "SELECT fiscal_year_end_month FROM companies WHERE ticker=?",
        (ticker,),
    ).fetchone()
    conn.close()
    return row[0] if row else 12


def _sanitize_filename(name: str) -> str:
    """Lowercase, replace non [a-z0-9._-] with '-', collapse consecutive dashes."""
    name = name.lower()
    name = re.sub(r"[^a-z0-9._-]", "-", name)
    name = re.sub(r"-+", "-", name)
    return name.strip("-")


def _atomic_write(path: Path, content: bytes) -> None:
    """Write bytes to path via temp + rename for atomicity."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_bytes(content)
    tmp.replace(path)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
