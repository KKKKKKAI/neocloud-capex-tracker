"""6-K earnings releases: classifier, release search, fetch and watcher
wiring, against a faked EDGAR (plus one opt-in live test)."""
from __future__ import annotations

from datetime import date
from types import SimpleNamespace

import pytest

from capex import paths
from capex.extract.router import _chain_for_form
from capex.fetch import sec_6k
from capex.fetch.dispatcher import _compute_period_token, fetch_and_record
from capex.fetch.errors import SourceUnavailableError
from capex.monitor import pipeline
from capex.monitor.watcher import ERROR, HIT, KNOWN, NOT_YET, edgar_cik, poll_for_row
from capex.organize.namer import PeriodDerivationError, compute_period_token
from capex.read.sections import PRESS_RELEASE_HEAD, PRESS_RELEASE_TAIL, get_extraction_sections

BIDU_CIK = "0001329099"
HIGHLIGHTS = "<p>" + "Revenue from AI Cloud grew strongly in the quarter. " * 20 + "</p>"


def release(title="Baidu Announces Second Quarter 2026 Results", period="June 30, 2026",
            prior="June 30, 2025", quarter="second quarter", extra=""):
    """A minimal earnings release exhibit."""
    return f"""<html><head><title>ex99</title></head><body>
<p><b>{title}</b></p>
<p>BEIJING, August 18, 2026 -- The Company today announced its unaudited
financial results for the {quarter} ended {period}.</p>
{extra}
<p>Total revenues were RMB 32.1 billion for the three months ended {period},
compared with RMB 31.0 billion for the three months ended {prior}.</p>
<table><tr><td>Condensed Consolidated Statements of Income</td></tr>
<tr><td>Three months ended</td><td>{prior}</td><td>{period}</td></tr></table>
</body></html>"""


def text_of(html: str) -> str:
    from capex.read.text import html_to_text
    return html_to_text(html)


AGM_NOTICE = """<p>NOTICE OF ANNUAL GENERAL MEETING</p>
<p>NOTICE IS HEREBY GIVEN that the annual general meeting of shareholders will be held
to receive the audited financial statements for the year ended December 31, 2025.</p>"""
CIRCULAR = """<p>THIS CIRCULAR IS IMPORTANT AND REQUIRES YOUR IMMEDIATE ATTENTION</p>
<p>Proposed amendments to the articles of association.</p>"""
BUYBACK_RETURN = """<p>Next Day Disclosure Return</p>
<p>(Equity issuer - changes in issued shares or treasury shares)</p>"""


# ---- classification ----------------------------------------------------------------

def test_quarterly_release_scores_every_cue():
    assert sec_6k.classify(text_of(release())) == (True, 9)


def test_title_broken_across_lines_still_matches():
    html = release(title="Baidu Announces</b></p>\n<p><b>Second Quarter 2026</b></p>\n<p><b>Results")
    assert sec_6k.classify(text_of(html)) == (True, 9)


def test_fiscal_quarter_naming_non_december_year():
    html = release(title="Alibaba Group Announces June Quarter 2026 Results",
                   quarter="quarter")
    text = text_of(html)
    assert sec_6k.classify(text)[0]
    assert sec_6k.derive_period(text, date(2026, 8, 20)) == date(2026, 6, 30)


def test_q4_and_full_year_release():
    html = release(title="GDS Holdings Limited Reports Fourth Quarter and Full Year 2025 Results",
                   period="December 31, 2025", prior="December 31, 2024",
                   quarter="fourth quarter and full year")
    text = text_of(html)
    assert sec_6k.classify(text)[0]
    assert sec_6k.derive_period(text, date(2026, 3, 17)) == date(2025, 12, 31)


def test_general_meeting_mentioned_in_the_highlights_is_still_a_release():
    # BIDU's Q2 2026 release announces an EGM ~2,000 characters in.
    egm = HIGHLIGHTS + ("<p>The Company will convene an extraordinary general meeting of "
                        "shareholders on August 26, 2026.</p>")
    assert sec_6k.classify(text_of(release(extra=egm))) == (True, 9)


@pytest.mark.parametrize("html", [AGM_NOTICE, CIRCULAR, BUYBACK_RETURN],
                         ids=["agm-notice", "circular", "buyback-return"])
def test_notices_circulars_and_returns_are_certainly_not_releases(html):
    is_release, score = sec_6k.classify(text_of(html))
    assert not is_release and score <= sec_6k.CERTAINLY_NOT_A_RELEASE


def test_derive_period_ignores_comparatives_and_reads_abbreviations():
    text = text_of(release(period="June 30, 2026", prior="June 30, 2025"))
    text += "\nthree and six months ended June 30, 2025 " * 5   # prior year, many times
    assert sec_6k.derive_period(text, date(2026, 8, 18)) == date(2026, 6, 30)
    nbis = "Revenue for the three and six months ended Sept. 30, 2026 rose."
    assert sec_6k.derive_period(nbis, date(2026, 11, 12)) == date(2026, 9, 30)
    assert sec_6k.derive_period("no period phrases here", date(2026, 8, 18)) is None


# ---- a fake EDGAR ------------------------------------------------------------------

@pytest.fixture
def edgar(monkeypatch):
    """Filings served from memory; every request is recorded."""
    site = SimpleNamespace(json={}, bytes={}, requests=[], filings=[])

    def get_json(url, **_):
        site.requests.append(url)
        return site.json[url]

    def get_bytes(url, **_):
        site.requests.append(url)
        return site.bytes[url]

    def add(accession, filed, exhibits):
        """exhibits: [(name, size, html-or-None)]."""
        base = sec_6k.accession_base(BIDU_CIK, accession)
        site.json[base + "index.json"] = {"directory": {"item": [
            {"name": name, "size": str(size)} for name, size, _ in exhibits]}}
        for name, _, html in exhibits:
            if html is not None:
                site.bytes[base + name] = html.encode()
        site.filings.append({"accessionNumber": accession, "filingDate": filed,
                             "reportDate": filed, "primaryDocument": "cover.htm", "form": "6-K"})

    def submissions():
        rows = sorted(site.filings, key=lambda f: f["filingDate"], reverse=True)
        cols = ("accessionNumber", "filingDate", "reportDate", "primaryDocument", "form")
        return {"filings": {"recent": {c: [f[c] for f in rows] for c in cols}}}

    def index_requested(accession):
        return sec_6k.accession_base(BIDU_CIK, accession) + "index.json" in site.requests

    def downloaded(accession):
        base = sec_6k.accession_base(BIDU_CIK, accession)
        return any(u.startswith(base) and not u.endswith("index.json") for u in site.requests)

    monkeypatch.setattr(sec_6k.sec_http, "get_json", get_json)
    monkeypatch.setattr(sec_6k.sec_http, "get_bytes", get_bytes)
    site.add, site.submissions = add, submissions
    site.index_requested, site.downloaded = index_requested, downloaded
    return site


def _q2_world(edgar):
    """BIDU after its Q2 2026 release, with a buyback return filed later."""
    edgar.add("acc-buyback", "2026-08-26", [("ex99-1.htm", 12_000, BUYBACK_RETURN)])
    edgar.add("acc-q2", "2026-08-18", [("dex991.htm", 250_000, release()),
                                       ("dex992.htm", 90_000, "<p>Supplemental tables</p>")])
    edgar.add("acc-q1", "2026-05-18", [("dex991.htm", 240_000, release(
        title="Baidu Announces First Quarter 2026 Results", period="March 31, 2026",
        prior="March 31, 2025", quarter="first quarter"))])


def test_exhibit_items_keeps_ex99_html_only(edgar):
    edgar.add("acc-x", "2026-08-18", [
        ("d6k.htm", 5_000, None), ("dex992.htm", 50_000, None), ("dex991.htm", 250_000, None),
        ("g1logo.jpg", 9_000, None), ("dex991.pdf", 300_000, None),
        ("tm1_ex99-3.htm", 60_000, None), ("0001-index.html", 3_000, None),
    ])
    items = sec_6k.exhibit_items(BIDU_CIK, "acc-x")
    assert [(i["name"], i["number"]) for i in items] == [
        ("dex991.htm", 1), ("dex992.htm", 2), ("tm1_ex99-3.htm", 3)]


def test_finds_the_release_and_rules_out_small_6ks(edgar):
    _q2_world(edgar)
    search = sec_6k.find_earnings_release(BIDU_CIK, date(2026, 6, 30), edgar.submissions())
    assert search.release == {"accessionNumber": "acc-q2", "filingDate": "2026-08-18",
                              "reportDate": "2026-06-30", "primaryDocument": "dex991.htm",
                              "form": "6-K"}
    assert search.score == 9
    assert [(f["accessionNumber"], why) for f, why in search.ignored] == [
        ("acc-buyback", "no EX-99 .htm exhibit large enough for a release")]
    assert not edgar.downloaded("acc-buyback")        # judged from the index alone
    assert not edgar.index_requested("acc-q1")        # filed before the quarter ended


def test_announced_day_is_checked_first(edgar):
    _q2_world(edgar)
    search = sec_6k.find_earnings_release(BIDU_CIK, date(2026, 6, 30), edgar.submissions(),
                                          report_date=date(2026, 8, 19))
    assert search.release["accessionNumber"] == "acc-q2"
    assert not edgar.index_requested("acc-buyback") and search.ignored == []


def test_remembered_filings_are_never_requested(edgar):
    _q2_world(edgar)
    sec_6k.find_earnings_release(BIDU_CIK, date(2026, 6, 30), edgar.submissions(),
                                 skip={"acc-buyback"})
    assert not edgar.index_requested("acc-buyback")


def test_borderline_exhibits_are_not_remembered(edgar):
    tables_only = ("<table><tr><td>Condensed Consolidated Balance Sheets</td></tr></table>"
                   "<p>for the quarter ended June 30, 2026</p>")
    edgar.add("acc-tables", "2026-08-18", [("ex99-1.htm", 80_000, tables_only)])
    search = sec_6k.find_earnings_release(BIDU_CIK, date(2026, 6, 30), edgar.submissions())
    assert search.release is None and search.ignored == []    # score 3: look again next time


def test_a_release_for_another_quarter_is_passed_over(edgar):
    _q2_world(edgar)
    edgar.add("acc-q3", "2026-10-20", [("dex991.htm", 260_000, release(
        title="Baidu Announces Third Quarter 2026 Results", period="September 30, 2026",
        prior="September 30, 2025", quarter="third quarter"))])
    search = sec_6k.find_earnings_release(BIDU_CIK, date(2026, 6, 30), edgar.submissions())
    assert search.release["accessionNumber"] == "acc-q2"
    assert "acc-q3" not in [f["accessionNumber"] for f, _ in search.ignored]


def test_same_day_siblings_keep_the_best_score(edgar):
    weaker = release(extra="").replace("Condensed Consolidated Statements of Income", "Summary")
    edgar.add("acc-a", "2026-08-18", [("ex99-1.htm", 70_000, weaker)])
    edgar.add("acc-b", "2026-08-18", [("dex991.htm", 250_000, release())])
    search = sec_6k.find_earnings_release(BIDU_CIK, date(2026, 6, 30), edgar.submissions(),
                                          report_date=date(2026, 8, 18))
    assert (search.release["accessionNumber"], search.score) == ("acc-b", 9)


def test_filings_outside_the_window_are_not_examined(edgar):
    edgar.add("acc-early", "2026-07-02", [("dex991.htm", 250_000, release())])
    edgar.add("acc-late", "2026-11-05", [("dex991.htm", 250_000, release())])
    search = sec_6k.find_earnings_release(BIDU_CIK, date(2026, 6, 30), edgar.submissions())
    assert search.release is None and edgar.requests == []


def test_find_latest_release_skips_newer_non_releases(edgar):
    _q2_world(edgar)
    latest = sec_6k.find_latest_release(BIDU_CIK, edgar.submissions(), today=date(2026, 9, 1))
    assert (latest["accessionNumber"], latest["reportDate"]) == ("acc-q2", "2026-06-30")


# ---- fetch, naming, extraction routing ------------------------------------------------

def _q2_filing(**over):
    return {"accessionNumber": "acc-q2", "filingDate": "2026-08-18", "reportDate": "2026-06-30",
            "primaryDocument": "dex991.htm", "form": "6-K", **over}


def test_fetch_release_saves_the_exhibits_as_one_file(capex_db, edgar):
    _q2_world(edgar)
    meta = sec_6k.fetch_release("BIDU", BIDU_CIK, _q2_filing())
    raw = paths.resolve_raw_path(meta["raw_path"])
    assert raw.name == "[2026.08.18][BIDU][Q2][6-K].htm"
    body = raw.read_text(encoding="utf-8")
    assert body.index("exhibit dex991.htm") < body.index("Baidu Announces") \
        < body.index("exhibit dex992.htm") < body.index("Supplemental tables")
    assert meta["exhibits"] == ["dex991.htm", "dex992.htm"]
    assert meta["source_url"] == sec_6k.accession_base(BIDU_CIK, "acc-q2") + "dex991.htm"
    assert (meta["form_type"], meta["period_of_report"], meta["accession_number"]) == (
        "6-K", "2026-06-30", "acc-q2")
    again = sec_6k.fetch_release("BIDU", BIDU_CIK, _q2_filing())
    assert again["raw_path"] == meta["raw_path"]           # same bytes: same file


def test_fetch_release_names_q4_and_fiscal_quarters(capex_db, edgar):
    edgar.add("acc-q4", "2027-02-26", [("dex991.htm", 250_000, "<p>Q4</p>")])
    q4 = sec_6k.fetch_release("BIDU", BIDU_CIK, _q2_filing(
        accessionNumber="acc-q4", filingDate="2027-02-26", reportDate="2026-12-31"))
    assert paths.resolve_raw_path(q4["raw_path"]).name == "[2027.02.26][BIDU][Q4][6-K].htm"
    baba = sec_6k.fetch_release("BABA", BIDU_CIK, _q2_filing(
        accessionNumber="acc-q4", filingDate="2026-08-20"))
    assert "[BABA][Q1][6-K]" in baba["raw_path"]            # FYE March: June is fiscal Q1


def test_fetch_and_record_a_release(capex_db, edgar):
    _q2_world(edgar)
    meta = fetch_and_record("BIDU", "6-K", db=capex_db, filing=_q2_filing())
    assert not meta["already_existed"]
    with capex_db.connect() as conn:
        row = conn.execute("SELECT * FROM source_documents WHERE id = ?", (meta["id"],)).fetchone()
    assert (row["period_token"], row["fiscal_year"], row["accession_number"]) == ("Q2", 2026, "acc-q2")
    assert (paths.resolve_raw_path(meta["raw_path"]).parent
            / "[2026.08.18][BIDU][Q2][6-K].htm.fetch.json").exists()
    assert fetch_and_record("BIDU", "6-K", db=capex_db, filing=_q2_filing())["already_existed"]


def test_period_tokens_give_6k_its_fourth_quarter():
    assert compute_period_token("6-K", "2026-12-31", 12) == "Q4"
    assert compute_period_token("6-K", "2026-06-30", 3) == "Q1"
    assert _compute_period_token("6-K", "2026-12-31", 12) == "Q4"
    with pytest.raises(PeriodDerivationError):
        compute_period_token("10-Q", "2026-12-31", 12)


def test_press_release_sections_keep_both_ends():
    short = "Highlights ... statements"
    assert get_extraction_sections({"_full": short}, "6-K") == {"Press release": short}
    long = "H" * PRESS_RELEASE_HEAD + "M" * 50_000 + "S" * PRESS_RELEASE_TAIL
    sections = get_extraction_sections({"_full": long}, "6-K")
    assert sections == {"Press release (highlights)": "H" * PRESS_RELEASE_HEAD,
                        "Press release (financial statements)": "S" * PRESS_RELEASE_TAIL}


def test_6k_extraction_goes_straight_to_the_llm():
    assert _chain_for_form(["xbrl", "6k_press", "llm"], "6-K") == ["llm"]
    assert _chain_for_form(["xbrl", "6k_press"], "6-K") == []
    assert _chain_for_form(["xbrl", "llm"], "10-Q") == ["xbrl", "llm"]


# ---- watcher ------------------------------------------------------------------------------

def _poll(db, edgar, **kw):
    return poll_for_row("BIDU", "6-K", "2026-06-30", db=db,
                        cache={"BIDU": edgar.submissions()}, **kw)


def test_poll_finds_the_release_and_skips_remembered_6ks(capex_db, edgar):
    _q2_world(edgar)
    with capex_db.mutating() as conn:
        conn.execute(
            "INSERT INTO filing_events (ticker, form_type, accession_number, filing_date, "
            "status, discovered_by, discovered_at, updated_at) VALUES "
            "('BIDU', '6-K', 'acc-buyback', '2026-08-26', 'ignored', 'calendar', 'x', 'x')")
    result = _poll(capex_db, edgar)
    assert (result.status, result.filing["accessionNumber"]) == (HIT, "acc-q2")
    assert not edgar.index_requested("acc-buyback") and result.ignored == []


def test_poll_reports_ruled_out_6ks_while_waiting(capex_db, edgar):
    edgar.add("acc-buyback", "2026-08-26", [("ex99-1.htm", 12_000, BUYBACK_RETURN)])
    result = _poll(capex_db, edgar)
    assert result.status == NOT_YET
    assert [f["accessionNumber"] for f, _ in result.ignored] == ["acc-buyback"]


@pytest.mark.parametrize("raw_path, phrase", [
    ("data/_sources/BIDU/_raw/x.htm", "period already recorded (source_documents id"),
    ("restated-virtual://BIDU/2026-06-30/acc-later", "from a later filing's comparatives"),
])
def test_poll_knows_recorded_periods(capex_db, edgar, raw_path, phrase):
    _q2_world(edgar)
    with capex_db.mutating() as conn:
        conn.execute(
            "INSERT INTO source_documents (ticker, form_type, filing_date, period_of_report, "
            "fiscal_year, period_token, sha256, raw_path, source, source_url, accession_number, "
            "fetched_at, fetcher_version, protocol_version) VALUES ('BIDU', '6-K', '2026-08-18', "
            "'2026-06-30', 2026, 'Q2', 'sha', ?, 'sec_edgar', 'u', 'other', 'x', 't', 'p')",
            (raw_path,))
    result = _poll(capex_db, edgar)
    assert result.status == KNOWN and phrase in result.detail


def test_poll_reports_sec_outages(capex_db, edgar, monkeypatch):
    _q2_world(edgar)

    def down(url, **_):
        raise SourceUnavailableError("sec_edgar", 503, "gave up after retries")

    monkeypatch.setattr(sec_6k.sec_http, "get_json", down)
    result = _poll(capex_db, edgar)
    assert result.status == ERROR and "gave up" in result.detail


def test_manual_6k_run_queues_the_latest_release(capex_db, edgar, monkeypatch):
    _q2_world(edgar)
    monkeypatch.setattr(pipeline, "today_eastern", lambda: date(2026, 9, 1))
    event_id = pipeline.enqueue_latest("BIDU", "6-K", db=capex_db,
                                       cache={"BIDU": edgar.submissions()})
    with capex_db.connect() as conn:
        event = dict(conn.execute("SELECT * FROM filing_events WHERE id = ?",
                                  (event_id,)).fetchone())
    assert (event["accession_number"], event["period_of_report"], event["primary_document"]) == (
        "acc-q2", "2026-06-30", "dex991.htm")


# ---- live -------------------------------------------------------------------------------

@pytest.mark.network
def test_live_gds_second_quarter_2026(capex_db):
    from capex.fetch.sec import get_submissions

    cik = edgar_cik("GDS", capex_db)
    search = sec_6k.find_earnings_release(cik, date(2026, 6, 30), get_submissions(cik),
                                          report_date=date(2026, 8, 19))
    assert search.release["accessionNumber"] == "0001104659-26-095498"
    assert search.release["reportDate"] == "2026-06-30"
