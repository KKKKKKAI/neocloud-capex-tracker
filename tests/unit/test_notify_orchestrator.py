"""End-to-end orchestrator: subscriber filter → email build → captured send."""
from __future__ import annotations

from pathlib import Path

import pytest

from capex.db.schema import Database, migrate
from capex.notify import notify_subscribers
from capex.notify.subscribers import add_subscriber


@pytest.fixture
def seeded_db(tmp_path: Path, monkeypatch) -> Database:
    db = Database(path=tmp_path / "test.db")
    migrate(db)
    with db.mutating() as conn:
        for k in ("revenue", "capital_expenditures", "operating_cash_flow"):
            conn.execute(
                "INSERT INTO metric_definitions (key, label, aliases, "
                "unit_default, description) "
                "VALUES (?, ?, '[]', 'USD_millions', '')",
                (k, k.replace("_", " ").title()),
            )
        conn.execute(
            "INSERT INTO companies (ticker, name, preferred_source, edgar_cik, "
            "fiscal_year_end_month, reporting_currency, synced_at) "
            "VALUES ('GOOGL','Alphabet Inc.','sec_edgar','0001652044',12,'USD',"
            "'2026-04-29T00:00:00+00:00')"
        )
        # Q1 FY2026 (current) + Q4 FY2025 (QoQ) + Q1 FY2025 (YoY) for revenue
        for fy, ptype, period_end, value, model in [
            (2025, "Q1", "2025-03-31", 90234, "xbrl-verified"),  # YoY
            (2025, "Q4", "2025-12-31", 96469, "xbrl-verified"),  # QoQ
            (2026, "Q1", "2026-03-31", 109896, "xbrl-verified"), # current
        ]:
            cur = conn.execute(
                "INSERT INTO source_documents (ticker, form_type, filing_date, "
                "period_of_report, fiscal_year, period_token, sha256, raw_path, "
                "source, source_url, accession_number, fetched_at, "
                "fetcher_version, protocol_version) VALUES "
                "('GOOGL', '10-Q', ?, ?, ?, ?, ?, ?, 'sec_edgar', "
                "'https://example/x.htm', 'acc', '2026-04-30T00:00:00+00:00', "
                "'test-1.0', '0.1.0')",
                (period_end, period_end, fy, ptype, f"sha-{fy}-{ptype}",
                 f"path/{fy}-{ptype}"),
            )
            sd = cur.lastrowid
            conn.execute(
                "INSERT INTO extractions (source_document_id, metric_key, value, "
                "value_text, unit, quote, locator_section, extraction_type, "
                "extracting_model, protocol_version, extracted_at, period_type, "
                "basis_period_months) VALUES (?, 'revenue', ?, '$x', "
                "'USD_millions', 'q', 'l', 'direct', ?, '0.1.0-draft', "
                "'2026-04-30T00:00:00+00:00', ?, 3)",
                (sd, float(value), model, ptype),
            )
    return db


def test_orchestrator_sends_to_matching_subscriber(
    seeded_db: Database, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("NOTIFY_SUBSCRIBERS_PATH", str(tmp_path / "subs.yaml"))
    add_subscriber("alice@example.com", path=tmp_path / "subs.yaml")
    add_subscriber("msft-only@example.com",
                   tickers=["MSFT"], path=tmp_path / "subs.yaml")

    captured: list[dict] = []

    def fake_send(*, to_email, subject, html_body, text_body, **kw):
        captured.append({
            "to": to_email, "subject": subject,
            "html": html_body, "text": text_body,
        })

    summary = notify_subscribers(
        results=[{
            "status": "success",
            "ticker": "GOOGL",
            "period": "2026-03-31",
            "filed": "2026-04-30",
        }],
        db=seeded_db,
        send_fn=fake_send,
    )

    assert summary["sent"] == 1
    assert summary["errors"] == []
    assert len(captured) == 1
    assert captured[0]["to"] == "alice@example.com"
    assert "GOOGL" in captured[0]["subject"]
    assert "Q1 FY2026" in captured[0]["subject"]
    assert "$109" in captured[0]["html"]      # revenue value present
    assert "alphabet" in captured[0]["html"].lower()


def test_orchestrator_skips_failed_results(
    seeded_db: Database, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("NOTIFY_SUBSCRIBERS_PATH", str(tmp_path / "subs.yaml"))
    add_subscriber("alice@example.com", path=tmp_path / "subs.yaml")

    captured = []
    summary = notify_subscribers(
        results=[
            {"status": "timeout", "ticker": "GOOGL", "period": "2026-03-31"},
            {"status": "fetch_failed", "ticker": "META", "period": "2026-03-31"},
        ],
        db=seeded_db,
        send_fn=lambda **kw: captured.append(kw),
    )
    assert summary["sent"] == 0
    assert captured == []


def test_orchestrator_no_subscribers_returns_clean_summary(
    seeded_db: Database, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("NOTIFY_SUBSCRIBERS_PATH", str(tmp_path / "subs.yaml"))
    summary = notify_subscribers(
        results=[{"status": "success", "ticker": "GOOGL",
                  "period": "2026-03-31", "filed": "2026-04-30"}],
        db=seeded_db,
        send_fn=lambda **kw: None,
    )
    assert summary["sent"] == 0
    assert summary["skipped"] == 1
    assert summary["errors"] == []


def test_orchestrator_logs_send_errors_continues(
    seeded_db: Database, tmp_path: Path, monkeypatch
):
    monkeypatch.setenv("NOTIFY_SUBSCRIBERS_PATH", str(tmp_path / "subs.yaml"))
    add_subscriber("alice@example.com", path=tmp_path / "subs.yaml")
    add_subscriber("bob@example.com", path=tmp_path / "subs.yaml")

    calls = []

    def flaky_send(*, to_email, **kw):
        calls.append(to_email)
        if to_email == "alice@example.com":
            raise RuntimeError("simulated SMTP rejection")

    summary = notify_subscribers(
        results=[{"status": "success", "ticker": "GOOGL",
                  "period": "2026-03-31", "filed": "2026-04-30"}],
        db=seeded_db,
        send_fn=flaky_send,
    )
    # bob still got the email even though alice failed
    assert calls == ["alice@example.com", "bob@example.com"]
    assert summary["sent"] == 1
    assert len(summary["errors"]) == 1
    assert summary["errors"][0]["subscriber"] == "alice@example.com"
