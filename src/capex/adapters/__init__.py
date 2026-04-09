"""Model-agnostic backend adapters (Anthropic, Google, OpenAI, ...).

The extraction layer depends only on the adapter protocol defined in
base.py, never on a concrete SDK. Swapping models is a config change.

Implementation lands in Phase 3.
"""
