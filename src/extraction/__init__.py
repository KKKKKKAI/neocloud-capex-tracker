"""Layer 3 — Extraction.

Model-agnostic interface that turns ingested documents into structured rows
matching the protocol contract defined in `src/protocol/`. Pluggable model
backends live in `src/adapters/`; this layer must never depend on a specific
model SDK.

See docs/SYSTEM_DESIGN.md §4, §5 for layer contract and protocol.
"""
