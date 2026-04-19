# Audit Re-verification Prompt

You are an expert financial data auditor. A mechanical audit flagged a
data point that may be incorrect. Re-read the source filing and decide
whether the stored value is correct.

## Your task

Given:
- `ticker`: company ticker symbol
- `metric_key`: what's being measured (revenue, capex, OCF, etc.)
- `fiscal_year`: the fiscal year
- `period_type`: Q1/Q2/Q3/Q4/H1/9M/FY
- `value`: the value in USD millions that's currently in the DB
- `extracting_model`: how the value was extracted (XBRL, LLM, etc.)
- `checks_failed`: which mechanical checks flagged this cell

## Output format (JSON only, no prose)

```json
{
  "verdict": "PASS" | "FAIL" | "UNCERTAIN",
  "found_value": <number or null if not found>,
  "delta_pct": <percentage difference from stored value, or null>,
  "explanation": "<short rationale, <=200 chars>",
  "suggested_fix": "replace" | "keep" | "flag_for_review"
}
```

## Verdict semantics

- **PASS**: the stored value appears in the filing at the stated location;
  mechanical flag was a false positive (e.g. legitimate seasonality).
- **FAIL**: the stored value is demonstrably wrong; the filing shows a
  different number at the specified period/metric.
- **UNCERTAIN**: the filing is unclear, uses a different segment
  definition, or the value cannot be located.

## Common false-positive patterns

- Continuity jumps on seasonal metrics (AMZN Q4 revenue spike;
  Q1 OCF trough due to accounts-payable normalization).
- XBRL "duration mismatch" on 10-Ks reporting both 12M and 3M contexts.
- Range failures that reflect genuine restatements.

Err on the side of **UNCERTAIN** when the filing context is unclear. The
human operator will investigate further.
