---
name: query-line-item
description: Look up a specific financial metric for a company and period. Returns the value with full provenance (verbatim quote, section reference, source file). Use this skill when the user asks "what was MSFT's FY2025 capex?", "how much did Amazon spend on PP&E?", "show me Google's revenue", or any question about a specific financial data point for a tracked company. The skill checks the extractions cache first and only invokes read-and-extract on a cache miss, so repeated queries are instant.
---

# query-line-item

## Purpose

This is the **user-facing front door** for data retrieval. Given a question
about a specific metric for a specific company, it resolves the question,
checks the `extractions` cache in the database, and returns the value with
full provenance. If the value hasn't been extracted yet, it triggers the
`read-and-extract` skill to fill the cache.

## When to trigger

- "What was MSFT's FY2025 capex?"
- "How much did Amazon spend on property and equipment?"
- "Show me Google's revenue for the latest year"
- "What's Alibaba's operating cash flow?"
- Any question asking for a specific financial data point for a tracked company

## Inputs

```
{
  "ticker": "MSFT",
  "metric": "capex",                    # natural language or canonical key
  "period": "FY2025"                    # optional, defaults to latest
}
```

The `metric` field can be a natural-language phrase ("capex", "capital
expenditures") or a canonical key ("capital_expenditures"). Resolution
uses the `aliases` array in `metric_definitions` to map from user phrases
to canonical keys.

## Outputs

```json
{
  "ticker": "MSFT",
  "period": "2025-06-30",
  "metric_key": "capital_expenditures",
  "value": 88000,
  "value_text": "$88.0 billion",
  "unit": "USD_millions",
  "quote": "purchases of property and equipment were $88.0 billion",
  "section_ref": "Item 8 - Consolidated Statements of Cash Flows",
  "source_path": "data/_sources/MSFT/_raw/msft-20250630.htm",
  "sha256": "99d693f...",
  "cache_hit": true,
  "xbrl_anchor_match": true
}
```

## Implementation

Lives in `src/capex/query/line_items.py`.

## What this skill does NOT do

- No extraction. It calls read-and-extract on cache miss.
- No file I/O (beyond DB queries).
- No model calls (beyond triggering read-and-extract).
