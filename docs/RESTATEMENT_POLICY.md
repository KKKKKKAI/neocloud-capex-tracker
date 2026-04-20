# Restatement Policy

Listed companies routinely **restate** historical quarterly and annual
values when they reorganize reporting lines. The filing where a number
*first* appeared is rarely the filing where it's most accurate; the
*latest* filing that carries that period is. This doc explains how
we capture, prioritise, and cite restated values.

---

## The problem

Concrete example (MSFT Intelligent Cloud):

| Period | As-reported in original 10-K | Restated in next fiscal-year 10-K |
|---|---|---|
| FY2024 Intelligent Cloud | $105,362M | $87,464M |

MSFT narrowed the Intelligent Cloud scope in FY2024 (moved Office365
and Dynamics out to Productivity & Business Processes). The FY2025
10-K's retrospective segment table restates FY2024 lower; comparing
the two values gives a clean apples-to-apples series.

If we keep citing the original 10-K we draw an artificial "drop"
between FY2024 (old scope) and FY2025 (new scope). If we cite the
restated figure from the FY2025 10-K, the curve is smooth and the
Excel cell comment points at the filing that's authoritative today.

---

## The rule: newest `source_documents.filing_date` wins

Every selector in the pipeline orders by
`source_documents.filing_date DESC` as the tiebreaker when multiple
extraction rows exist for the same
`(ticker, metric_key, fiscal_year, period_type)` cell.

Hardened selectors:

| File | Function |
|---|---|
| `src/capex/exporters/interactive_chart.py` | `_load_annual`, `_load_quarterly` |
| `src/capex/exporters/charts.py` | PNG annual query |
| `src/capex/audit/orchestrator.py` | `load_cells` |
| `src/capex/extract/reconcile.py` | `_load_existing` + `_group_rows` |

Because Excel cell comments and chart hover tooltips read the row's
`source_document.source_url`, `locator_section`, and `quote`, the
citation auto-rewires whenever a restated row wins: **2023Q3 figure
restated in a 2024Q3 10-Q cites the 2024Q3 download link**, without
any change in the Excel exporter itself.

---

## How restatements are captured — LLM dual-agent, one flow

Every time the headless LLM extractor reads a filing, **it extracts
both the current period AND every prior-period comparative** shown
in the same table / income statement. Each period is verified
independently by Agent B, and each becomes its own extraction row.

### Agent A prompt (multi-period schema)

`src/capex/verification/prompts/agent_a.txt` instructs the LLM to
emit a list of periods:

```json
{
  "found": true,
  "periods": [
    {
      "role": "primary",
      "label": "FY2025",
      "period_of_report": "2025-06-30",
      "basis_period_months": 12,
      "value": 106265,
      "unit": "USD_millions",
      "excerpts": [{"text": "...", "location": "Segment table", "role": "primary_value"}],
      "reasoning": "current period"
    },
    {
      "role": "comparative",
      "label": "FY2024 (restated)",
      "period_of_report": "2024-06-30",
      "basis_period_months": 12,
      "value": 87464,
      "unit": "USD_millions",
      "excerpts": [{"text": "...prior-year column...", "location": "Segment table", "role": "primary_value"}],
      "reasoning": "prior-year comparative column restated"
    }
  ]
}
```

### Agent B — blind per-period verification

`verify_period(agent_a_period, agent_b_result)` runs once per period.
Agent B sees only that period's excerpts (plus the period label) and
must independently deduce the value. If A and B disagree for a
period, that period is flagged needs-review and NOT written.

### Writer — one extraction row per verified period

`LLMHeadlessExtractor.extract()` iterates the verified periods and
writes one extraction per period:

- `role = "primary"` → standard write against the current filing's
  `source_document_id` with `extracting_model='llm-dual-agent'`.
- `role = "comparative"` → call
  `ensure_restated_source_doc(ticker, fy, restating_sd_id)` from
  `src/capex/extract/virtual_source_docs.py`. This creates a virtual
  `source_documents` row (form_type `'6-K'` to sidestep the
  `UNIQUE(ticker, form_type, period_of_report)` constraint on 10-Ks;
  `raw_path` prefixed `restated-virtual://`) whose `fiscal_year`
  matches the comparative period but whose `source_url` /
  `filing_date` / `accession_number` come from the restating filing.
  The extraction is written against that virtual row with
  `extracting_model='llm-dual-agent-restated@0.1.0'` and the
  period's own excerpts.

### Cascade via reconcile

Derived identities (`Q4 = FY − 9M`, `9M = Q1+Q2+Q3`, BIDU cloud =
`Total − Online − iQIYI`, etc.) run on whichever rows win the
selector. Because the `filing_date DESC` tiebreaker promotes restated
rows, reconcile automatically re-derives Q4 from restated Q1/Q2/Q3
whenever a later filing restates a prior quarter.

### No role for XBRL here

XBRL companyfacts is a fast-path for headline-metric time series,
but it is deliberately **not** used for restatement detection any
more. The previous implementation that wrote `restated-xbrl` rows
(preserving later-filed contexts with different `accn` + `val`) was
reverted: it bypassed dual-agent verification, carried no
filing-text quote for Excel citations, and was unreliable for
segment-level data. The LLM extractor above handles restatements
end-to-end.

---

## CLI

```
$ capex extract MSFT --metric cloud_segment_revenue
    # Standard extraction — reads the latest 10-K and writes one
    # primary + every verified prior-year comparative.

$ python scripts/sweep_llm_restatements.py --ticker MSFT \
     --metric cloud_segment_revenue --validate-only
    # Dry-run: prints Agent A output + verification per filing
    # without committing. Inspect before committing.

$ python scripts/sweep_llm_restatements.py --all-known-restaters
    # Sweep MSFT/ORCL/BABA/BIDU/GDS × headline + cloud metrics.

$ capex audit
    # Restatement section in the report lists cells where a later
    # filing's value materially differs from the original. Purely
    # observational — no applier; fresh extraction fixes forward.

$ python scripts/rollback_restatement_rows.py --apply
    # One-off utility — wipe every `restated-%` row + virtual
    # source_documents (used once to clean up legacy
    # `restated-xbrl` + `restated-segment` rows before switching
    # to the LLM-based flow).
```

---

## Cascade into Excel citations (no code change needed)

When the selector promotes the restated row:

- `exporters/excel.py` reads `source_url` from the joined
  `source_documents` → Shift+F2 cell-comment link becomes the
  *restating* filing (via the virtual row's copied `source_url`).
- `exporters/citations.py` reads `locator_section` + `quote` from the
  extraction row → cell-comment body quotes the *restating*
  filing's table row.

The rule is enforced by data, not by special-case code in the
exporter.

---

## Policy per company

Companies with a history of restatement keep a `restatement_policy`
block in `data/seeds/coverage.yaml`:

```yaml
MSFT:
  restatement_policy:
    prefer_restated: true
    known_restatements:
      - "FY2018 segment_reorg (Commercial Cloud → Intelligent Cloud)"
      - "FY2024 segment_reorg (Intelligent Cloud scope narrowed)"
    note: |
      The latest 10-K's retrospective segment table is authoritative —
      prior-year values in the current 10-K supersede the originals.
```

The treatments viewer (`docs/treatments.html`) renders the policy
block per company card. Tracked today: MSFT, ORCL, BABA, BIDU, GDS.

---

## Known gaps / follow-ups

- **HKEX interim reports (HK-IR, HK-AR)** carry restatements too,
  but many aren't yet fetched on disk. The LLM extractor handles
  them automatically once `raw_path` is populated.
- **Full retroactive sweep** across every ticker × metric (not just
  known-restaters) — deferred to manage LLM cost.
- **MSFT supplemental 8-K** between fiscal years — MSFT often
  publishes a stand-alone 8-K with restated quarterly breakdowns
  after a segment reorg, before the next 10-K. Worth fetching + running
  through the same LLM path.

---

## Implementation map

| Concern | File |
|---|---|
| Multi-period extraction prompt | `src/capex/verification/prompts/agent_a.txt` |
| Agent B prompt (per period) | `src/capex/verification/prompts/agent_b.txt` |
| Parser + per-period verifier | `src/capex/verification/dual_agent.py` |
| Writer (one row per period) | `src/capex/extract/extractors/llm_headless.py` |
| Virtual source_doc helper | `src/capex/extract/virtual_source_docs.py` |
| Selector hardening | `interactive_chart._load_annual/_load_quarterly`, `charts.py`, `audit/orchestrator.load_cells`, `reconcile._load_existing/_group_rows` |
| Audit reporter | `src/capex/audit/restatement.py` |
| Bulk sweep driver | `scripts/sweep_llm_restatements.py` |
| Cleanup utility (one-off) | `scripts/rollback_restatement_rows.py` |
| Coverage policy | `data/seeds/coverage.yaml` |
| Treatments viewer surface | `src/capex/exporters/treatments_html.py` |
