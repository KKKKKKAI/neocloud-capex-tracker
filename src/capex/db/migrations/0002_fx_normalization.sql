-- FX normalization: store reporting currency + USD-converted values.
--
-- Every extracted value is stored in BOTH local currency (value) and
-- USD (value_usd) with the FX rate + date recorded alongside. For USD
-- reporters, value_usd = value and fx_rate = 1.0.

-- 1. Add reporting_currency to companies (mirrors _identity.yaml)
ALTER TABLE companies ADD COLUMN reporting_currency TEXT NOT NULL DEFAULT 'USD';

-- 2. FX rates table — cached period-end exchange rates
CREATE TABLE IF NOT EXISTS fx_rates (
    currency_pair  TEXT NOT NULL,   -- e.g. 'CNY/USD' (1 CNY = ? USD)
    rate_date      TEXT NOT NULL,   -- ISO date (period-end date)
    rate           REAL NOT NULL,   -- the exchange rate
    source         TEXT NOT NULL DEFAULT 'frankfurter',  -- 'frankfurter' / 'ecb' / 'manual'
    fetched_at     TEXT NOT NULL,
    PRIMARY KEY (currency_pair, rate_date)
);

-- 3. Add FX columns to extractions
ALTER TABLE extractions ADD COLUMN value_usd REAL;
ALTER TABLE extractions ADD COLUMN fx_rate REAL;
ALTER TABLE extractions ADD COLUMN fx_rate_date TEXT;
ALTER TABLE extractions ADD COLUMN reporting_currency TEXT NOT NULL DEFAULT 'USD';
