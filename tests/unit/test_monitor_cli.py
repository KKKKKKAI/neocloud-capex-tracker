"""`capex monitor` argument handling and exit codes."""
from __future__ import annotations

from types import SimpleNamespace

import pytest

from capex.monitor import pipeline, run


@pytest.fixture
def fake(capex_db, monkeypatch):
    """Replace the pipeline run; record its kwargs; return a chosen summary."""
    state = SimpleNamespace(calls=[], summary=pipeline.RunSummary())

    def fake_run(**kwargs):
        state.calls.append(kwargs)
        return state.summary

    monkeypatch.setattr(pipeline, "run_watcher", fake_run)
    return state


@pytest.mark.parametrize("stop_kind, outcome_status, code", [
    (None, "extracted", 0),
    (None, "partial", 3),
    (None, "failed", 3),
    ("deferred", None, 75),
    ("auth", None, 77),
    ("config", None, 1),
])
def test_exit_codes(fake, stop_kind, outcome_status, code):
    fake.summary = pipeline.RunSummary(stop_kind=stop_kind,
                                       stopped="why" if stop_kind else None)
    if outcome_status:
        fake.summary.outcomes.append(pipeline.EventOutcome(
            1, "MSFT", "10-Q", "2026-09-30", "2026-10-28", outcome_status))
    assert run.main(["--catch-up"]) == code


def test_catch_up_flags(fake):
    run.main(["--catch-up", "--since", "2026-05-01", "--sweep", "--dry-run"])
    kwargs = fake.calls[0]
    assert (kwargs["since"], kwargs["sweep"], kwargs["dry_run"]) == ("2026-05-01", True, True)


def test_since_needs_a_value(fake):
    with pytest.raises(SystemExit, match="--since needs a value"):
        run.main(["--catch-up", "--since"])


def test_ticker_mode_queues_the_latest_filing(fake, monkeypatch):
    queued = []
    monkeypatch.setattr(pipeline, "enqueue_latest",
                        lambda ticker, form, db: queued.append((ticker, form)) or 42)
    run.main(["iren"])
    assert queued == [("IREN", "10-Q")]         # form taken from the watchlist
    assert fake.calls[0]["event_ids"] == [42]


def test_ticker_mode_dry_run_touches_nothing(fake, monkeypatch, capsys):
    monkeypatch.setattr(pipeline, "enqueue_latest", lambda *a, **k: pytest.fail("queued"))
    assert run.main(["MSFT", "10-K", "--dry-run"]) == 0
    assert "would queue" in capsys.readouterr().out
    assert fake.calls == []


def test_unknown_flag_is_an_error(fake):
    assert run.main(["--frobnicate"]) == 1
    assert fake.calls == []
