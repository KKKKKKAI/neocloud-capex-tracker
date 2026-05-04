"""Subscriber YAML round-trip + filter logic."""
from __future__ import annotations

from pathlib import Path

from capex.notify.subscribers import (
    Subscriber,
    add_subscriber,
    filter_for_ticker,
    load_subscribers,
    remove_subscriber,
    save_subscribers,
    set_enabled,
)


def test_load_missing_file_returns_empty_list(tmp_path: Path):
    subs = load_subscribers(tmp_path / "missing.yaml")
    assert subs == []


def test_round_trip(tmp_path: Path):
    p = tmp_path / "subs.yaml"
    save_subscribers([
        Subscriber(email="a@example.com"),
        Subscriber(email="b@example.com",
                   tickers=["MSFT", "GOOGL"],
                   metrics=["revenue"], enabled=False),
    ], p)
    out = load_subscribers(p)
    assert len(out) == 2
    assert out[0].email == "a@example.com"
    assert out[0].tickers == ["*"]
    assert out[0].metrics == ["*"]
    assert out[0].enabled is True
    assert out[1].tickers == ["MSFT", "GOOGL"]
    assert out[1].enabled is False


def test_add_idempotent(tmp_path: Path):
    p = tmp_path / "subs.yaml"
    add_subscriber("alice@x.com", path=p)
    add_subscriber("alice@x.com", tickers=["MSFT"], path=p)  # update
    add_subscriber("bob@x.com", path=p)
    subs = load_subscribers(p)
    assert len(subs) == 2
    alice = next(s for s in subs if s.email == "alice@x.com")
    assert alice.tickers == ["MSFT"]


def test_remove(tmp_path: Path):
    p = tmp_path / "subs.yaml"
    add_subscriber("a@x.com", path=p)
    add_subscriber("b@x.com", path=p)
    assert remove_subscriber("a@x.com", path=p) is True
    assert remove_subscriber("a@x.com", path=p) is False
    subs = load_subscribers(p)
    assert [s.email for s in subs] == ["b@x.com"]


def test_set_enabled(tmp_path: Path):
    p = tmp_path / "subs.yaml"
    add_subscriber("a@x.com", path=p)
    assert set_enabled("a@x.com", False, path=p) is True
    assert load_subscribers(p)[0].enabled is False
    assert set_enabled("missing@x.com", False, path=p) is False


def test_filter_for_ticker_respects_wildcard_and_enabled():
    subs = [
        Subscriber(email="all@x.com"),
        Subscriber(email="msft@x.com", tickers=["MSFT"]),
        Subscriber(email="googl-disabled@x.com",
                   tickers=["GOOGL"], enabled=False),
    ]
    msft = {s.email for s in filter_for_ticker(subs, "MSFT")}
    assert msft == {"all@x.com", "msft@x.com"}
    googl = {s.email for s in filter_for_ticker(subs, "GOOGL")}
    assert googl == {"all@x.com"}    # disabled excluded


def test_subscriber_metric_match():
    s = Subscriber(email="a@x.com", metrics=["revenue", "capex"])
    assert s.matches_metric("revenue")
    assert not s.matches_metric("operating_cash_flow")
    s2 = Subscriber(email="b@x.com")  # default ["*"]
    assert s2.matches_metric("anything")
