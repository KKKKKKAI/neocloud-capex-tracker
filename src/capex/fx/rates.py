"""FX rate fetcher and cache.

Fetches historical exchange rates from frankfurter.app (free, no API
key, backed by ECB data) and caches them in the `fx_rates` table. Once
fetched, a rate is never re-fetched — the cache is permanent.

Usage:
    from capex.fx.rates import get_fx_rate

    # Get the CNY→USD rate as of 2025-03-31 (Alibaba's FYE)
    rate = get_fx_rate("CNY", "USD", "2025-03-31")
    # rate ≈ 0.137  (1 CNY = 0.137 USD)

    value_cny = 140905  # operating income in CNY millions
    value_usd = value_cny * rate  # ≈ 19,304 USD millions

The rate is the amount of target currency per 1 unit of source currency.
So for CNY→USD: 1 CNY = rate USD.

Frankfurter.app API:
    GET https://api.frankfurter.app/2025-03-31?from=CNY&to=USD
    → {"amount":1.0,"base":"CNY","date":"2025-03-31","rates":{"USD":0.13759}}

If the exact date is a weekend/holiday, frankfurter returns the nearest
business day rate. The actual date used is in the response and we store
that as the `rate_date`.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.request
from datetime import datetime, timezone

from ..db import Database

FRANKFURTER_URL = "https://api.frankfurter.app/{date}?from={source}&to={target}"


def get_fx_rate(
    source_currency: str,
    target_currency: str,
    rate_date: str,
    *,
    db: Database | None = None,
) -> float:
    """Get the exchange rate from source to target currency on a given date.

    Args:
        source_currency: ISO 4217 code (e.g. "CNY")
        target_currency: ISO 4217 code (e.g. "USD")
        rate_date: ISO date string (e.g. "2025-03-31")

    Returns:
        The rate: 1 unit of source = rate units of target.

    If the exact date is a weekend/holiday, the nearest business day
    rate is used. The actual date is stored in the cache.
    """
    if source_currency == target_currency:
        return 1.0

    db = db or Database()
    pair = f"{source_currency}/{target_currency}"

    # Check cache first
    cached = _get_cached_rate(db, pair, rate_date)
    if cached is not None:
        return cached

    # Fetch from frankfurter.app
    rate, actual_date = _fetch_from_frankfurter(source_currency, target_currency, rate_date)

    # Cache the result
    _cache_rate(db, pair, actual_date, rate)

    # Also cache under the requested date if it differs (so future lookups
    # for the same requested date hit the cache without another API call)
    if actual_date != rate_date:
        _cache_rate(db, pair, rate_date, rate)

    return rate


def normalize_to_usd(
    value: float | None,
    reporting_currency: str,
    period_date: str,
    *,
    db: Database | None = None,
) -> tuple[float | None, float, str]:
    """Convert a value from reporting currency to USD.

    Returns:
        (value_usd, fx_rate, fx_rate_date)
        For USD reporters: (value, 1.0, period_date)
        For non-USD: (value * rate, rate, actual_rate_date)
    """
    if value is None:
        return None, 1.0, period_date

    if reporting_currency == "USD":
        return value, 1.0, period_date

    rate = get_fx_rate(reporting_currency, "USD", period_date, db=db)
    return round(value * rate, 2), rate, period_date


def get_company_currency(ticker: str, *, db: Database | None = None) -> str:
    """Look up a company's reporting currency from the DB."""
    db = db or Database()
    with db.connect() as conn:
        row = conn.execute(
            "SELECT reporting_currency FROM companies WHERE ticker = ?",
            (ticker,),
        ).fetchone()
        return row["reporting_currency"] if row else "USD"


# ----------------------------------------------------------------------------
# Internal
# ----------------------------------------------------------------------------


def _get_cached_rate(db: Database, pair: str, rate_date: str) -> float | None:
    with db.connect() as conn:
        row = conn.execute(
            "SELECT rate FROM fx_rates WHERE currency_pair = ? AND rate_date = ?",
            (pair, rate_date),
        ).fetchone()
        return row["rate"] if row else None


def _cache_rate(db: Database, pair: str, rate_date: str, rate: float) -> None:
    with db.mutating() as conn:
        conn.execute(
            """
            INSERT INTO fx_rates (currency_pair, rate_date, rate, source, fetched_at)
            VALUES (?, ?, ?, 'frankfurter', ?)
            ON CONFLICT(currency_pair, rate_date) DO UPDATE SET
                rate = excluded.rate,
                fetched_at = excluded.fetched_at
            """,
            (pair, rate_date, rate, _now_iso()),
        )


def _fetch_from_frankfurter(
    source: str, target: str, date: str
) -> tuple[float, str]:
    """Fetch a rate from frankfurter.app. Returns (rate, actual_date)."""
    url = FRANKFURTER_URL.format(date=date, source=source, target=target)
    req = urllib.request.Request(url, headers={"User-Agent": "neocloud-capex-tracker"})
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
            rate = data["rates"][target]
            actual_date = data.get("date", date)
            return float(rate), actual_date
    except urllib.error.HTTPError as e:
        raise RuntimeError(f"frankfurter.app error: {e.code} for {url}") from e
    except (KeyError, json.JSONDecodeError) as e:
        raise RuntimeError(f"frankfurter.app unexpected response for {url}: {e}") from e


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")
