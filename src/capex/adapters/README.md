# Model Adapters — Migration Guide

## Current state (v1 / Phase 3)

Extraction runs inside **Claude Code** — the conversational runtime the
developer is already using. No separate API key is needed. The extraction
flow is:

1. User invokes the `read-and-extract` skill in Claude Code
2. Claude Code reads filing sections (via `src/capex/read/`)
3. Claude Code follows the prompt template (from `src/capex/extract/prompts/`)
4. Claude Code produces structured JSON matching the protocol schema
5. The results are passed to `src/capex/extract/writer.py` which writes
   to the database

Step 5 (the writer) is **adapter-agnostic**: it accepts a list of result
dicts and writes DB rows regardless of which model produced them.

## When to add a programmatic adapter

You need a programmatic adapter when ANY of these are true:

- You want extraction to run **without a human in a Claude Code session**
  (cron jobs, CI pipelines, GitHub Actions watcher)
- You want to **compare models** (run the same prompt through Claude,
  Gemini, GPT-4o and compare output quality)
- You want to **control cost** (use a cheaper model for headline metrics,
  reserve the expensive model for AI-attribution analysis)

## How to add an adapter

### 1. Implement the `ModelBackend` protocol

```python
# src/capex/adapters/anthropic.py
from .base import ModelBackend

class AnthropicBackend(ModelBackend):
    name = "claude-opus-4-8"
    version = "2026-07"

    def __init__(self):
        import anthropic
        self.client = anthropic.Client()  # reads ANTHROPIC_API_KEY env var

    def extract(self, system: str, user: str) -> str:
        response = self.client.messages.create(
            model=self.name,
            max_tokens=4096,
            system=system,
            messages=[{"role": "user", "content": user}],
        )
        return response.content[0].text
```

### 2. Wire the adapter into the extractor

```python
# src/capex/extract/extractor.py (Phase 3.5)
from capex.adapters.anthropic import AnthropicBackend
from capex.extract.writer import write_extractions
from capex.read.sections import get_extraction_sections

def extract_headless(source_document_id, metric_keys, backend=None):
    backend = backend or AnthropicBackend()
    sections = ...  # load from the filing
    prompt = ...    # format the prompt template
    raw_json = backend.extract(system="...", user=prompt)
    results = json.loads(raw_json)
    return write_extractions(results)
```

### 3. Add the CLI integration

```python
# In src/capex/cli/main.py, update the extract subcommand:
def _extract_command(argv):
    # If ANTHROPIC_API_KEY is set, use the headless extractor
    # Otherwise, print instructions to invoke the skill in Claude Code
    ...
```

### 4. Environment variables

| Env var | Purpose | Default |
|---|---|---|
| `ANTHROPIC_API_KEY` | Anthropic API authentication | (none — required for anthropic adapter) |
| `CAPEX_EXTRACT_MODEL` | Which model to use | `claude-opus-4-8` |
| `CAPEX_EXTRACT_BACKEND` | Which adapter to use | `claude-code` (v1) / `anthropic` (Phase 3.5) |

## What stays the same when you swap adapters

- `src/capex/extract/writer.py` — the DB write layer
- `src/capex/extract/prompts/` — the prompt templates
- `src/capex/protocol/v0_1_0.py` — the result schema
- `src/capex/read/` — the section parser
- `data/db/` — the schema and all existing data
- `skills/read-and-extract/SKILL.md` — the skill contract

The adapter is the ONLY thing that changes. Everything else is
adapter-agnostic by design.
