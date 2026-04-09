"""Layer 7 — Validation pipeline.

Six sub-layers, cheapest first:

    A. Schema tests            (unit tests; in tests/schema/)
    B. Golden-set regression   (fixtures; in tests/golden/)
    C. Provenance verification (Python substring match of quote vs source)
    D. Consistency rules       (implemented as Excel formulas, not code)
    E. Eval agent              (LLM audit; different model family)
    F. Cross-source triangulation (Excel formulas across sources)

Layers C and E live in this package. Layers A/B are in tests/. Layers D/F
live in the Excel template as formulas.

See docs/SYSTEM_DESIGN.md §5.3.
"""
