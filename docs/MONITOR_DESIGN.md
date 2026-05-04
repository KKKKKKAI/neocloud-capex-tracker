# Fiscal Calendar Monitor — System Design

**Status:** Designed, not yet implemented.  
**Created:** 2026-04-15  
**Owner:** @KKKKKKAI

---

## Overview

Automated pipeline that detects new quarterly/annual filings on the
exact day they're released, downloads them, runs LLM dual-agent
extraction, cross-checks against XBRL, and publishes results — all
without human intervention.

Runs locally via WSL cron. Uses Claude Code CLI (`claude -p`) for LLM
calls with Pro Max subscription (no API key needed). Pluggable to
Gemini CLI or Codex CLI as alternatives.

---

## Architecture

```
┌──────────────────────────────────────────────────────────┐
│              LOCAL WSL CRON (user's machine)               │
│                                                            │
│  ┌─────────────┐    ┌──────────────┐    ┌───────────────┐ │
│  │ 1. CALENDAR  │───▶│ 2. WATCHER   │───▶│ 3. EXTRACT    │ │
│  │ Weekly pull  │    │ On earnings  │    │ Python orch.  │ │
│  │ Alpha Vantage│    │ day: poll    │    │ calls claude  │ │
│  │ → exact date │    │ SEC every    │    │ -p for Agent  │ │
│  │ per company  │    │ 30 min for   │    │ A + Agent B   │ │
│  │              │    │ the filing   │    │ (text in/out) │ │
│  │              │    │              │    │ XBRL cross-chk│ │
│  └─────────────┘    └──────────────┘    └──────┬────────┘ │
│                                                │          │
│  ┌─────────────────────────────────────────────▼────────┐ │
│  │ 4. PUBLISH                                            │ │
│  │ Python: openpyxl → Excel  |  plotly → charts          │ │
│  │ subprocess: git push      |  gh issue create          │ │
│  └───────────────────────────────────────────────────────┘ │
└──────────────────────────────────────────────────────────┘
```

### Key design: Python orchestrates, CLI is text-in/text-out only

The Python monitor handles ALL I/O, DB writes, git operations, and
file management directly. Claude Code (or Gemini/Codex) is invoked
ONLY as a stateless text generator via `-p` (print) mode — it receives
a prompt string, returns text to stdout, and exits. **No tool
permissions, no bash execution, no file edits by the LLM.** This means
the pipeline runs unattended via cron without any approval prompts.

```
Python orchestrator (no approval needed)
  ├── urllib: poll SEC API, download filings
  ├── sqlite3: read/write DB  
  ├── subprocess: "claude -p <prompt>" → stdout text → parse JSON
  ├── openpyxl: generate Excel
  └── subprocess: git add/commit/push, gh issue create
```

---

## Part 1: Earnings Calendar Integration

> **Scope.** This entire section is about discovering forward earnings
> *dates* — i.e. answering "when will GOOGL next report?" so the
> watcher knows when to start polling SEC. The third-party feed
> described below (Alpha Vantage) is used SOLELY for that. It never
> sees a financial value, never extracts data, never writes to
> `extractions` or `extraction_evidence`. All actual data extraction
> is our own LLM dual-agent framework — see Part 3.

### How companies announce earnings dates

Companies announce their exact earnings release date weeks in advance
on their investor relations pages. Financial data aggregators compile
these into programmatic calendars. The SEC filing (10-Q, 10-K, 6-K)
typically appears the same day as the earnings announcement.

### Source: Alpha Vantage (free tier)

```
GET https://www.alphavantage.co/query?function=EARNINGS_CALENDAR&horizon=3month&apikey={FREE_KEY}
```

Returns CSV with exact company-announced dates:
```csv
symbol,name,reportDate,fiscalDateEnding,estimate,currency
MSFT,Microsoft Corp,2026-01-28,2025-12-31,3.23,USD
AMZN,Amazon.com Inc,2026-02-06,2025-12-31,1.51,USD
```

### New table: `fiscal_calendar` (migration 0006)

```sql
CREATE TABLE fiscal_calendar (
    id                  INTEGER PRIMARY KEY AUTOINCREMENT,
    ticker              TEXT NOT NULL REFERENCES companies(ticker),
    report_date         TEXT NOT NULL,     -- announced earnings date
    fiscal_date_ending  TEXT NOT NULL,     -- quarter-end date
    form_type           TEXT,              -- expected: 10-Q, 10-K, 20-F, 6-K
    status              TEXT NOT NULL DEFAULT 'upcoming',
                                          -- upcoming → detected → fetched → extracted
    source              TEXT NOT NULL DEFAULT 'alpha_vantage',
    updated_at          TEXT NOT NULL,
    UNIQUE(ticker, fiscal_date_ending)
);
```

### Calendar sync module

**File:** `src/capex/monitor/calendar.py`

```python
def sync_earnings_calendar(api_key: str, db=None):
    """Pull upcoming earnings dates from Alpha Vantage.
    
    Fetches 3-month horizon CSV, filters to our 13 tracked tickers,
    upserts into fiscal_calendar table.
    """

def get_todays_earnings(db=None) -> list[dict]:
    """Return companies reporting earnings today."""

def get_upcoming_earnings(days=7, db=None) -> list[dict]:
    """Return companies reporting in the next N days."""
```

For Chinese/HK companies not covered by Alpha Vantage:
- SEC EDGAR 8-K monitoring (Item 2.02 = earnings announced)
- HKEX announcements feed (for Tencent)
- Manual entry in fiscal_calendar table

---

## Part 2: CLI Adapter Layer

**File:** `src/capex/adapters/cli_backend.py`

Pluggable wrapper for Claude Code, Gemini CLI, and Codex CLI. All
use `-p` (print) mode — prompt in, text out, no tool permissions.

```python
class CLIBackend:
    """Call LLM via CLI tools. Text-in/text-out only, no permissions."""
    
    TOOLS = {
        "claude": {"cmd": ["claude", "-p"]},
        "gemini": {"cmd": ["gemini", "-p"]},
        "codex":  {"cmd": ["codex", "-p"]},
    }
    
    def __init__(self, tool="claude"):
        self.tool = tool
        self.config = self.TOOLS[tool]
    
    def extract(self, system: str, user: str) -> str:
        """Send prompt, return stdout. No tool permissions needed."""
        prompt = f"{system}\n\n{user}"
        result = subprocess.run(
            [*self.config["cmd"], prompt],
            capture_output=True, text=True, timeout=300,
        )
        return result.stdout
    
    @classmethod
    def detect_available(cls) -> str | None:
        """Auto-detect which CLI tool is installed."""
        for tool in ["claude", "gemini", "codex"]:
            try:
                r = subprocess.run([tool, "--version"], capture_output=True, timeout=5)
                if r.returncode == 0:
                    return tool
            except FileNotFoundError:
                continue
        return None
```

---

## Part 3: Extraction Flow — LLM-First, XBRL-Validates

The extraction chain is inverted from the original design:

```
                       NEW (LLM-first)
                       ─────────────────
                       1. LLM Agent A reads filing → value + context
                       2. LLM Agent B blindly verifies from context
                       3. Compare A vs B → verified or queued
                       4. XBRL cross-check (validation, not extraction)
                       5. Write to DB if verified + XBRL matches
```

This ensures every data point has:
- LLM-extracted citations and evidence (works for ALL metrics)
- XBRL cross-check where available (catches LLM errors)
- Dual-agent verification (catches hallucination)

### Per-filing call pattern (the cost-shape that the watcher uses)

For one new 10-Q the LLM traffic is **1 Agent A call + N Agent B calls** (one per
metric, batched across periods), not N × (Agent A + Agent B):

```
1 × Agent A multi-metric prompt   (~106K input chars: filing once + 6 metric specs)
6 × Agent B excerpt-only prompts  (~1K input chars each, batched across periods)
```

This is ~5–6× cheaper and faster than calling Agent A once per metric.
Per-metric fallback fires automatically for any metric the multi-metric pass
can't satisfy (parse failure, Agent B says insufficient, no verified primary
or comparative). Worst case = today's per-metric cost; common case = the
cheap path above.

### Headless dual-agent extractors

**Per-filing (auto-update path):** `src/capex/extract/extractors/llm_headless_filing.py`

```python
class LLMHeadlessFilingExtractor:
    def extract_filing(ticker, form_type, period, metric_keys, *, backend, db=None):
        """One Agent A multi-metric call + one Agent B per metric.
        
        Returns {metric_key: [verified candidates] | None}.
        None signals the router to fall back to the per-metric path.
        """
```

**Per-metric (fallback + PEL re-extract + restatement sweep):**
`src/capex/extract/extractors/llm_headless.py`

```python
class LLMHeadlessExtractor:
    def extract(ticker, metric_key, period, *, backend, db=None):
        """Full per-metric dual-agent extraction with 3-attempt
        context-broadening retry loop. Used when multi-metric needs
        a more careful read of one specific metric."""
```

The router's `extract_filing()` (in `src/capex/extract/router.py`) is the
single entry point that wires both: XBRL pre-filter → multi-metric Agent A →
per-metric fallback for anything that didn't verify.

---

## Part 4: Filing Watcher

**File:** `src/capex/monitor/watcher.py`

Polls SEC EDGAR / HKEX on earnings days until the filing appears.

```python
def watch_and_extract(ticker, form_type, *, backend, db=None,
                      max_polls=48, interval=1800):
    """Poll SEC every 30 min (up to 24 hours). Extract when found.
    
    Triggered by cron on the earnings date from fiscal_calendar.
    """
    for attempt in range(max_polls):
        latest = poll_sec_latest(ticker, form_type)
        if latest and not already_in_db(ticker, form_type, latest["period"], db):
            # 1. Download filing
            fetch_and_record(ticker, form_type, db=db)
            # 2. LLM dual-agent extraction for all metrics
            for metric_key in get_metrics_for_company(ticker):
                extract_with_dual_agent(ticker, metric_key,
                    latest["period"], backend=backend, db=db)
            # 3. XBRL cross-check
            run_xbrl_validation(ticker, latest["period"], db=db)
            # 4. Update calendar status
            update_calendar_status(ticker, latest["period"], "extracted", db)
            return {"status": "success", "ticker": ticker}
        time.sleep(interval)
    return {"status": "timeout", "ticker": ticker}
```

---

## Part 5: Monitor Runner (Cron Entry Point)

**File:** `src/capex/monitor/run.py`

```python
"""Daily monitor — called by cron. No human interaction needed."""

def main():
    db = Database()
    tool = CLIBackend.detect_available()
    if not tool:
        print("ERROR: No LLM CLI tool found (claude/gemini/codex)")
        return
    backend = CLIBackend(tool)
    
    # Check if any company reports earnings today
    todays = get_todays_earnings(db=db)
    if not todays:
        print("No earnings today.")
        return
    
    results = []
    for earning in todays:
        result = watch_and_extract(
            earning["ticker"], infer_form_type(earning),
            backend=backend, db=db,
        )
        results.append(result)
    
    # Regenerate outputs
    if any(r["status"] == "success" for r in results):
        export_workbook()
        generate_charts()
        subprocess.run(["git", "add", "-A"])
        subprocess.run(["git", "commit", "-m",
            f"data(auto): {date.today()} earnings update"])
        subprocess.run(["git", "push"])
    
    # Create GitHub issue
    create_github_issue(results)
```

---

## Part 6: Local Cron Setup

### WSL crontab entries

```bash
# Weekly: sync earnings calendar (Sundays at 8 AM local)
0 8 * * 0  cd /path/to/neocloud-capex-tracker && PYTHONPATH=src python3 -m capex.monitor.calendar_sync

# Daily weekdays: check if today is an earnings day (6 AM local)
0 6 * * 1-5  cd /path/to/neocloud-capex-tracker && PYTHONPATH=src python3 -m capex.monitor.run
```

### CLI commands

```bash
capex calendar sync             # pull next 3 months from Alpha Vantage
capex calendar show             # show upcoming earnings dates
capex calendar show --week      # this week only

capex monitor MSFT              # manually trigger for one company
capex monitor --all-today       # process all companies with earnings today
```

---

## Module Structure (new files)

```
src/capex/
  adapters/
    cli_backend.py              # Claude Code / Gemini / Codex CLI wrapper
  monitor/
    __init__.py
    calendar.py                 # Alpha Vantage earnings calendar sync
    watcher.py                  # filing detection + SEC/HKEX polling
    run.py                      # daily cron entry point
  extract/
    extractors/
      llm_headless.py           # headless dual-agent via CLI backend
    router.py                   # MODIFY: accept backend, LLM-first flow
  db/
    migrations/
      0006_fiscal_calendar.sql  # fiscal_calendar table
```

---

## Implementation Phases

```
Phase 1 (adapter + calendar):
  1. cli_backend.py — CLI wrapper with auto-detection
  2. Migration 0006 — fiscal_calendar table
  3. calendar.py — Alpha Vantage sync

Phase 2 (headless extraction):
  4. llm_headless.py — dual-agent via CLI backend
  5. Update router.py — LLM-first, XBRL validates

Phase 3 (monitor):
  6. watcher.py — SEC/HKEX polling loop
  7. run.py — cron entry point
  8. CLI commands (capex calendar, capex monitor)

Phase 4 (deploy):
  9. Register free Alpha Vantage API key
  10. Set up WSL cron jobs
  11. Test: capex monitor MSFT
  12. Verify end-to-end: calendar → detect → fetch → extract → publish
```

## Prerequisites

- Claude Code CLI installed and authenticated (Pro Max subscription)
- Free Alpha Vantage API key (register at alphavantage.co)
- WSL cron enabled (`sudo service cron start`)
- `gh` CLI authenticated (for GitHub issue creation)

## Cost

- Alpha Vantage: free (5 calls/minute, 500/day — we need ~1/week)
- SEC EDGAR: free (10 requests/sec, no key)
- Claude Code: included in Pro Max subscription
- **Total: $0/month**
