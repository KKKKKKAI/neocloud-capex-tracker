"""Quarterly earnings releases furnished on SEC Form 6-K.

Foreign private issuers (NBIS, GDS, BIDU, BABA) file no 10-Q. Each
quarter's results arrive as a 6-K whose EX-99.1 exhibit is the earnings
press release — but most 6-Ks are something else (share-buyback returns
almost daily for BIDU, AGM notices, HKEX announcements, circulars).
EDGAR gives a 6-K no period either: its "reportDate" is just the filing
date. So for a fiscal period we:

1. look at 6-Ks filed 7-120 days after the period end;
2. read each filing's index and take its first EX-99 .htm exhibit big
   enough to be a release (images, PDFs and one-page notices are out);
3. classify the text (title "... Announces/Reports ... Quarter ...
   Results", "financial results for the ...", condensed statements),
   rejecting AGM notices, circulars and exchange returns by their title;
4. read the period from phrases like "three and six months ended June
   30, 2026" and accept it only within a few days of the expected end.

Filings that aren't releases are reported back so the caller can
remember them and never download them again.
"""
from __future__ import annotations

import hashlib
import re
from collections import Counter
from dataclasses import dataclass, field
from datetime import date
from typing import Any

from .. import paths
from ..organize.namer import canonical_name, compute_period_token
from ..read.text import html_to_text
from . import sec_http
from .sec import (
    _atomic_write,
    _build_metadata,
    _get_fye_month,
    list_filings,
)

ARCHIVE_BASE = "https://www.sec.gov/Archives/edgar/data"
MIN_LAG_DAYS = 7            # a release comes weeks after the quarter closes
MAX_LAG_DAYS = 120          # ...and never four months later
PERIOD_TOLERANCE_DAYS = 7   # derived period vs. the expected period end
MIN_RELEASE_BYTES = 40_000  # buyback returns / notices are ~8-30 KB
MAX_EXTRA_EXHIBITS = 3
CERTAINLY_NOT_A_RELEASE = 0  # classification scores at or below this are remembered
TITLE_ZONE_CHARS = 600       # where "notice"/"circular" cues count against a filing

_EX99 = re.compile(r"ex(?:hibit)?[-_ .]?99(?:[-_ .d])?(\d*)", re.IGNORECASE)
_MONTHS = {m: i for i, m in enumerate(
    ["jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"], 1)}
_MONTH = (r"(January|February|March|April|May|June|July|August|September|October|"
          r"November|December|Jan|Feb|Mar|Apr|Jun|Jul|Aug|Sept|Sep|Oct|Nov|Dec)\.?")
_ENDED = re.compile(
    r"(?:(?:three|3)(?:\s+and\s+(?:six|nine|twelve))?\s+months"
    r"|(?:first|second|third|fourth|march|june|september|december)?\s*quarter)"
    r"\s+ended\s+" + _MONTH + r"\s+(\d{1,2}),?\s+(\d{4})",
    re.IGNORECASE,
)
_TITLE = re.compile(
    r"\b(announces?|reports?)\b[^\n]{0,80}?\b(quarter|quarterly|fiscal year|full year)\b"
    r"[^\n]{0,60}?\bresults\b",
    re.IGNORECASE,
)
_RESULTS_INTRO = re.compile(r"\bfinancial results for the\b", re.IGNORECASE)
_STATEMENTS = re.compile(
    r"condensed consolidated (statements? of (operations|income|comprehensive|cash flows)"
    r"|balance sheets?)",
    re.IGNORECASE,
)
_NOT_A_RELEASE = re.compile(
    r"annual general meeting|extraordinary general meeting|notice of (the )?(annual|extraordinary)"
    r"|proxy (statement|form)|next day disclosure return|monthly return of equity"
    r"|this circular is important",
    re.IGNORECASE,
)


@dataclass
class ReleaseSearch:
    """Outcome of find_earnings_release()."""
    release: dict[str, str] | None = None     # a filing dict, see below
    ignored: list[tuple[dict[str, str], str]] = field(default_factory=list)
    score: int = 0


def accession_base(cik: str, accession: str) -> str:
    return f"{ARCHIVE_BASE}/{int(cik)}/{accession.replace('-', '')}/"


def exhibit_items(cik: str, accession: str) -> list[dict[str, Any]]:
    """EX-99 .htm exhibits of a filing, in exhibit order (99.1 first)."""
    index = sec_http.get_json(accession_base(cik, accession) + "index.json")
    items = []
    for item in index.get("directory", {}).get("item", []):
        name = item.get("name", "")
        if not name.lower().endswith((".htm", ".html")):
            continue  # images, PDFs, index pages
        m = _EX99.search(name)
        if not m or name.lower().endswith("-index.html"):
            continue
        try:
            size = int(item.get("size") or 0)
        except ValueError:
            size = 0
        items.append({"name": name, "size": size, "number": int(m.group(1) or 1)})
    return sorted(items, key=lambda i: (i["number"], i["name"]))


def classify(text: str) -> tuple[bool, int]:
    """(is an earnings release, score) for an exhibit's text.

    Whitespace is collapsed first: converted HTML often breaks a title
    like "Baidu Announces / Second Quarter 2026 Results" across lines.
    """
    head = " ".join(text[:6_000].split())[:4_000]
    body = " ".join(text[:60_000].split())
    score = 0
    if _TITLE.search(head):
        score += 4
    if _RESULTS_INTRO.search(head):
        score += 2
    if _STATEMENTS.search(body):
        score += 2
    if _ENDED.search(head):
        score += 1
    # Notices and circulars say what they are in their title; a real
    # release may mention an upcoming general meeting in its highlights.
    if _NOT_A_RELEASE.search(head[:TITLE_ZONE_CHARS]):
        score -= 6
    return score >= 4, score


def derive_period(text: str, filed: date) -> date | None:
    """The quarter end a release reports on, from "... ended <date>" phrases.

    Only dates up to MAX_LAG_DAYS before the filing count (that drops the
    prior-year comparatives); the most frequent one wins, ties → latest.
    """
    counts: Counter[date] = Counter()
    for m in _ENDED.finditer(text[:40_000]):
        month = _MONTHS.get(m.group(1)[:3].lower())
        try:
            end = date(int(m.group(3)), month, int(m.group(2)))
        except (TypeError, ValueError):
            continue
        if end <= filed and (filed - end).days <= MAX_LAG_DAYS:
            counts[end] += 1
    if not counts:
        return None
    return max(counts, key=lambda d: (counts[d], d))


def find_earnings_release(
    cik: str,
    period_end: date,
    submissions: dict,
    *,
    report_date: date | None = None,
    skip: set[str] | frozenset[str] = frozenset(),
) -> ReleaseSearch:
    """Look for the 6-K earnings release covering the quarter ending `period_end`.

    On success `release` is a filing dict for the dispatcher:
    accessionNumber, filingDate, reportDate (= the derived period end),
    primaryDocument (= the release exhibit), form ("6-K").
    """
    search = ReleaseSearch()
    lo, hi = MIN_LAG_DAYS, MAX_LAG_DAYS
    pool = []
    for filing in list_filings(submissions, "6-K"):
        lag = (date.fromisoformat(filing["filingDate"]) - period_end).days
        if lo <= lag <= hi and filing["accessionNumber"] not in skip:
            pool.append(filing)
    if report_date is not None:  # try the announced day's filings first
        pool.sort(key=lambda f: abs((date.fromisoformat(f["filingDate"]) - report_date).days))

    best: tuple[int, dict[str, str]] | None = None
    for filing in pool:
        if best and filing["filingDate"] != best[1]["filingDate"]:
            break  # same-day siblings were compared; stop downloading
        exhibits = exhibit_items(cik, filing["accessionNumber"])
        main = next((e for e in exhibits if e["size"] >= MIN_RELEASE_BYTES or not e["size"]), None)
        if main is None:
            search.ignored.append((filing, "no EX-99 .htm exhibit large enough for a release"))
            continue
        raw = sec_http.get_bytes(accession_base(cik, filing["accessionNumber"]) + main["name"])
        text = html_to_text(raw.decode("utf-8", errors="replace"))
        is_release, score = classify(text)
        if not is_release:
            if score <= CERTAINLY_NOT_A_RELEASE:
                # Only certain rejections are remembered; a borderline
                # exhibit is looked at again next time rather than being
                # hidden for good by a classifier mistake.
                search.ignored.append((filing, f"not an earnings release (score {score})"))
            continue
        period = derive_period(text, date.fromisoformat(filing["filingDate"]))
        if period is None or abs((period - period_end).days) > PERIOD_TOLERANCE_DAYS:
            continue  # a release, but for another period: leave it for that row
        found = {
            "accessionNumber": filing["accessionNumber"],
            "filingDate": filing["filingDate"],
            "reportDate": period.isoformat(),
            "primaryDocument": main["name"],
            "form": "6-K",
        }
        if best is None or score > best[0]:
            best = (score, found)
    if best:
        search.score, search.release = best
    return search


def fetch_release(ticker: str, cik: str, filing: dict[str, str]) -> dict[str, Any]:
    """Download a release (from find_earnings_release) into the raw archive.

    The release exhibit and up to MAX_EXTRA_EXHIBITS further EX-99 .htm
    exhibits (financial statements, shareholder letters) are saved as one
    file, `[filed][T][Qn][6-K].htm`, so extraction sees them together.
    source_url is the release exhibit's SEC URL (external citation).
    """
    accession = filing["accessionNumber"]
    base = accession_base(cik, accession)
    main = filing["primaryDocument"]
    others = [e["name"] for e in exhibit_items(cik, accession) if e["name"] != main]
    names = [main, *others[:MAX_EXTRA_EXHIBITS]]
    parts = []
    for name in names:
        parts.append(f"\n<!-- capex: exhibit {name} -->\n".encode())
        parts.append(sec_http.get_bytes(base + name))
    body = b"".join(parts)
    sha256 = hashlib.sha256(body).hexdigest()

    period = filing["reportDate"]
    token = compute_period_token("6-K", period, _get_fye_month(ticker), ticker=ticker)
    raw_dir = paths.sources_dir() / ticker / "_raw"
    raw_dir.mkdir(parents=True, exist_ok=True)
    raw_path = raw_dir / canonical_name(filing["filingDate"], ticker, token, "6-K", ".htm")
    if raw_path.exists() and hashlib.sha256(raw_path.read_bytes()).hexdigest() != sha256:
        raw_path = raw_path.with_name(f"{raw_path.stem}-{sha256[:8]}{raw_path.suffix}")
    if not raw_path.exists():
        _atomic_write(raw_path, body)

    metadata = _build_metadata(
        ticker=ticker, raw_path=raw_path, sha256=sha256, source_url=base + main,
        accession=accession, form_type="6-K", filing_date=filing["filingDate"],
        period_of_report=period,
    )
    metadata["exhibits"] = names
    return metadata


def release_from_filing(cik: str, filing: dict[str, str]) -> dict[str, str] | None:
    """One chosen 6-K as a release: an operator override (admin "ingest"),
    so the classifier isn't consulted. Its first big EX-99 exhibit and the
    period read from it, or None when it has no exhibit or no period."""
    exhibits = exhibit_items(cik, filing["accessionNumber"])
    main = next((e for e in exhibits if e["size"] >= MIN_RELEASE_BYTES or not e["size"]),
                exhibits[0] if exhibits else None)
    if main is None:
        return None
    raw = sec_http.get_bytes(accession_base(cik, filing["accessionNumber"]) + main["name"])
    period = derive_period(html_to_text(raw.decode("utf-8", errors="replace")),
                           date.fromisoformat(filing["filingDate"]))
    if period is None:
        return None
    return {"accessionNumber": filing["accessionNumber"], "filingDate": filing["filingDate"],
            "reportDate": period.isoformat(), "primaryDocument": main["name"], "form": "6-K"}


def find_latest_release(cik: str, submissions: dict, *, today: date) -> dict[str, str] | None:
    """Most recent earnings release in the last MAX_LAG_DAYS + 90 days
    (for `capex fetch <T> 6-K` without a period)."""
    for filing in list_filings(submissions, "6-K"):
        filed = date.fromisoformat(filing["filingDate"])
        if (today - filed).days > MAX_LAG_DAYS + 90:
            break
        exhibits = exhibit_items(cik, filing["accessionNumber"])
        main = next((e for e in exhibits if e["size"] >= MIN_RELEASE_BYTES or not e["size"]), None)
        if main is None:
            continue
        raw = sec_http.get_bytes(accession_base(cik, filing["accessionNumber"]) + main["name"])
        text = html_to_text(raw.decode("utf-8", errors="replace"))
        if not classify(text)[0]:
            continue
        period = derive_period(text, filed)
        if period is None:
            continue
        return {"accessionNumber": filing["accessionNumber"], "filingDate": filing["filingDate"],
                "reportDate": period.isoformat(), "primaryDocument": main["name"], "form": "6-K"}
    return None
