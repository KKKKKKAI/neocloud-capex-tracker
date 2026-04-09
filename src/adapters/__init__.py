"""Model backend adapters.

Thin wrappers normalizing per-provider differences (JSON mode flags, tool use,
output parsing) so the rest of the system stays model-agnostic. Adding a new
provider means adding a new adapter that conforms to `base.ModelBackend`.

See docs/SYSTEM_DESIGN.md §5 (protocol) and §8.3 (multi-model routing deferral).
"""
