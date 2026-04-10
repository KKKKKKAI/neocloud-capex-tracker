"""Per-filing extraction worker.

The extraction layer has two parts:

1. **writer.py** — adapter-agnostic DB writer. Accepts structured result
   dicts (from any source — Claude Code, an API adapter, or a human
   typing JSON), validates them, writes extractions + validation_results
   + audit_log rows. Does not care how the results were produced.

2. **prompts/** — versioned prompt templates that tell the LLM what to
   extract. Used by Claude Code (v1) and by programmatic adapters (Phase 3.5).

In v1, the extraction flow is:
    1. User invokes the read-and-extract skill in Claude Code
    2. Claude Code reads the filing sections (via read/sections.py)
    3. Claude Code follows the prompt template and produces JSON
    4. Claude Code calls writer.write_extractions() to persist

In Phase 3.5, step 2-3 is replaced by a programmatic adapter call.
Step 1 and 4 stay the same. writer.py is the stable interface.
"""
