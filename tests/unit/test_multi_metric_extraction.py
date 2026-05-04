"""Unit + router-level tests for the multi-metric Agent A flow."""
from __future__ import annotations

import json

import pytest

from capex.extract import router
from capex.extract.base import ExtractionCandidate, ExtractResult
from capex.verification.dual_agent import (
    build_agent_a_multi_metric_prompt,
    parse_agent_a_multi_metric_response,
)

# ─── Prompt builder ──────────────────────────────────────────────────


def _spec(metric_key: str, *, sections: str = "") -> dict[str, str]:
    return {
        "metric_key": metric_key,
        "metric_description": f"Description for {metric_key}",
        "derivation_rules": f"DERIVATION_RULES_{metric_key}",
        "human_notes_block": f"HUMAN_NOTES_{metric_key}",
        "sections_text": sections,
    }


def test_multi_metric_prompt_includes_filing_text_once_and_every_metric():
    metrics = [_spec(k) for k in (
        "capital_expenditures", "revenue", "operating_cash_flow",
        "depreciation_amortization", "property_plant_equipment_net",
        "cloud_segment_revenue",
    )]
    metrics[0]["sections_text"] = "FILING_BODY_PLACEHOLDER"

    prompt = build_agent_a_multi_metric_prompt(
        company_name="Microsoft", form_type="10-Q",
        period="2026-03-31", metrics=metrics,
    )

    # Filing text appears once
    assert prompt.count("FILING_BODY_PLACEHOLDER") == 1, (
        "filing context must be included exactly once across the prompt"
    )

    # Every metric_key, description, derivation rule, and notes block appears
    for m in metrics:
        assert m["metric_key"] in prompt
        assert m["metric_description"] in prompt
        assert m["derivation_rules"] in prompt
        assert m["human_notes_block"] in prompt


def test_multi_metric_prompt_rejects_empty_metrics():
    with pytest.raises(ValueError):
        build_agent_a_multi_metric_prompt(
            company_name="x", form_type="10-Q",
            period="2026-03-31", metrics=[],
        )


# ─── Parser ──────────────────────────────────────────────────────────


def test_multi_metric_parser_splits_response_by_key():
    payload = {
        "metrics": [
            {
                "metric_key": "revenue",
                "found": True,
                "periods": [
                    {"role": "primary", "value": 12345,
                     "period_of_report": "2026-03-31",
                     "basis_period_months": 3, "excerpts": []},
                ],
                "reasoning": "from income statement",
            },
            {
                "metric_key": "capital_expenditures",
                "found": False,
                "periods": [],
                "reasoning": "not in this filing",
            },
        ]
    }
    out = parse_agent_a_multi_metric_response(
        json.dumps(payload),
        expected_keys=["revenue", "capital_expenditures",
                       "operating_cash_flow"],
    )
    assert set(out.keys()) == {
        "revenue", "capital_expenditures", "operating_cash_flow",
    }
    assert out["revenue"]["found"] is True
    assert out["revenue"]["periods"][0]["value"] == 12345
    assert out["capital_expenditures"]["found"] is False
    # Missing metric → None so caller can route it to fallback
    assert out["operating_cash_flow"] is None


def test_multi_metric_parser_handles_markdown_fences():
    payload = (
        '```json\n'
        '{"metrics":[{"metric_key":"revenue","found":true,'
        '"periods":[],"reasoning":""}]}\n'
        '```'
    )
    out = parse_agent_a_multi_metric_response(
        payload, expected_keys=["revenue"],
    )
    assert out["revenue"] is not None
    assert out["revenue"]["found"] is True


def test_multi_metric_parser_returns_all_none_on_total_garbage():
    out = parse_agent_a_multi_metric_response(
        "this is not JSON",
        expected_keys=["revenue", "capex"],
    )
    assert out == {"revenue": None, "capex": None}


def test_multi_metric_parser_skips_malformed_metric_entries():
    payload = json.dumps({
        "metrics": [
            {"metric_key": "revenue", "found": True, "periods": [],
             "reasoning": ""},
            "not a dict",                       # malformed
            {"found": True, "periods": []},     # missing metric_key
            {"metric_key": "", "found": True, "periods": []},  # empty key
        ]
    })
    out = parse_agent_a_multi_metric_response(
        payload, expected_keys=["revenue", "capital_expenditures"],
    )
    assert out["revenue"] is not None
    assert out["capital_expenditures"] is None


# ─── Router extract_filing fallback wiring ───────────────────────────


def _candidate(metric_key: str, *, value: float = 100.0) -> ExtractionCandidate:
    return ExtractionCandidate(
        source_document_id=1, metric_key=metric_key, value=value,
        value_text=f"{value}", unit="USD_millions",
        quote="q", locator_section="L", extraction_type="direct",
        extracting_model="llm-dual-agent", reporting_currency="USD",
        excerpts=[{"text": "x", "location": "y", "role": "primary_value"}],
        period_type="Q3", basis_period_months=3,
    )


class _FakeBackend:
    pass


def test_extract_filing_falls_back_per_metric_for_missing_results(monkeypatch):
    """Multi-metric pass returns 4 of 6 metrics; fallback fires for the other 2."""
    requested = [
        "capital_expenditures", "revenue", "operating_cash_flow",
        "depreciation_amortization", "property_plant_equipment_net",
        "cloud_segment_revenue",
    ]
    multi_returns = {
        # 4 satisfied
        "capital_expenditures": [_candidate("capital_expenditures")],
        "revenue":              [_candidate("revenue")],
        "operating_cash_flow":  [_candidate("operating_cash_flow")],
        "depreciation_amortization": [_candidate("depreciation_amortization")],
        # 2 need fallback
        "property_plant_equipment_net": None,
        "cloud_segment_revenue":        None,
    }

    class FakeFiling:
        def extract_filing(self, ticker, form_type, period, metric_keys,
                           *, backend, db=None):
            return {k: multi_returns[k] for k in metric_keys}

    monkeypatch.setattr(
        "capex.extract.extractors.llm_headless_filing.LLMHeadlessFilingExtractor",
        FakeFiling,
    )

    # XBRL pre-filter returns None (nothing satisfied) for all metrics
    monkeypatch.setattr(router, "_try_xbrl_only",
                        lambda *a, **kw: None)
    # Coverage: every metric has chain ["xbrl", "llm"]
    monkeypatch.setattr(router, "get_extraction_chain",
                        lambda t, m: ["xbrl", "llm"])
    # Capture writer calls
    write_calls: list[list[dict]] = []
    monkeypatch.setattr(
        router, "write_extractions",
        lambda results, **kw: (
            write_calls.append(results)
            or {"inserted": len(results), "overwritten": 0,
                "skipped_existing": 0, "errors": [], "ids": [1]}
        ),
    )

    fallback_calls: list[str] = []

    def fake_extract_metric(ticker, mk, **kw):
        fallback_calls.append(mk)
        return ExtractResult(
            status="success", extractor="llm",
            candidates=[_candidate(mk)],
            write_summary={"inserted": 1, "overwritten": 0,
                           "skipped_existing": 0, "errors": [], "ids": [42]},
            chain_tried=["xbrl", "llm"], verified=True,
        )

    monkeypatch.setattr(router, "extract_metric", fake_extract_metric)

    out = router.extract_filing(
        "MSFT", "10-Q", "2026-03-31",
        metric_keys=requested,
        write=True, backend=_FakeBackend(),
    )

    assert set(out.keys()) == set(requested)
    for mk in requested:
        assert out[mk].status == "success", f"{mk} should succeed"
    # Exactly the 2 missing-from-multi metrics fell back
    assert sorted(fallback_calls) == sorted([
        "property_plant_equipment_net", "cloud_segment_revenue",
    ])
    # 4 multi-pass writes (one per satisfied metric); fallback writes
    # happen inside the fake extract_metric, not via router's write.
    assert len(write_calls) == 4


def test_extract_filing_uses_xbrl_when_it_satisfies(monkeypatch):
    """If XBRL fires for a metric, it never enters the multi-metric pass."""
    monkeypatch.setattr(
        "capex.extract.extractors.llm_headless_filing.LLMHeadlessFilingExtractor",
        lambda: (_ for _ in ()).throw(  # would raise if instantiated
            AssertionError("multi-metric pass should not run"),
        ),
    )
    monkeypatch.setattr(router, "get_extraction_chain",
                        lambda t, m: ["xbrl", "llm"])
    monkeypatch.setattr(router, "_try_xbrl_only", lambda *a, **kw: ExtractResult(
        status="success", extractor="xbrl",
        candidates=[_candidate("revenue")], chain_tried=["xbrl"],
        verified=True,
    ))

    out = router.extract_filing(
        "MSFT", "10-Q", "2026-03-31",
        metric_keys=["revenue"], write=False, backend=_FakeBackend(),
    )
    assert out["revenue"].extractor == "xbrl"
    assert out["revenue"].status == "success"


def test_extract_filing_records_no_extractor_when_chain_empty(monkeypatch):
    monkeypatch.setattr(router, "get_extraction_chain", lambda t, m: [])
    out = router.extract_filing(
        "MSFT", "10-Q", "2026-03-31",
        metric_keys=["unknown_metric"], write=False, backend=_FakeBackend(),
    )
    assert out["unknown_metric"].status == "no_extractor"
