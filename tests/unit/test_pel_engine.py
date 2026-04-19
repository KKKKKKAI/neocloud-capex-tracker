"""Tests for the domain-agnostic PEL engine."""
from __future__ import annotations

import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "src"))

from capex.pel.formalizer import Formalizer, _extract_json_block
from capex.pel.protocol import Anomaly, Artifact
from capex.pel.session import ReviewCallbacks, ReviewSession


class FakeBackend:
    """Scripted ModelBackend for tests."""
    def __init__(self, replies: list[str]):
        self.replies = list(replies)
        self.calls: list[tuple[str, str]] = []

    def extract(self, system, user, response_schema=None):
        self.calls.append((system, user))
        if not self.replies:
            raise RuntimeError("FakeBackend out of replies")
        return self.replies.pop(0)


class ScriptedIO:
    """Scripted I/O for the ReviewSession."""
    def __init__(self, inputs: list[str], commits: list[bool]):
        self.inputs = list(inputs)
        self.commits = list(commits)
        self.rendered_anomalies: list[Anomaly] = []
        self.previews: list = []
        self.summaries: list = []

    def render_anomaly(self, a, i, total):
        self.rendered_anomalies.append(a)

    def read_input(self, prompt):
        return self.inputs.pop(0) if self.inputs else ""

    def render_preview(self, result):
        self.previews.append(result)

    def ask_commit(self):
        return self.commits.pop(0) if self.commits else False

    def render_summary(self, outcome):
        self.summaries.append(outcome)

    def as_callbacks(self) -> ReviewCallbacks:
        return ReviewCallbacks(
            render_anomaly=self.render_anomaly,
            read_input=self.read_input,
            render_preview=self.render_preview,
            ask_commit=self.ask_commit,
            render_summary=self.render_summary,
        )


# ---- JSON extraction ----------------------------------------------

def test_extract_json_from_fenced_block():
    text = "Here's your answer:\n```json\n{\"a\": 1, \"b\": 2}\n```\nDone."
    assert _extract_json_block(text) == {"a": 1, "b": 2}


def test_extract_json_from_bare_object():
    text = "Sure thing: {\"key\": \"value\"} — all done."
    assert _extract_json_block(text) == {"key": "value"}


def test_extract_json_nested_braces():
    text = '```\n{"outer": {"inner": [1,2,3]}}\n```'
    assert _extract_json_block(text) == {"outer": {"inner": [1, 2, 3]}}


def test_extract_json_none_when_no_json():
    assert _extract_json_block("just prose, no structure") is None


def test_extract_json_none_for_empty():
    assert _extract_json_block("") is None


# ---- Formalizer ---------------------------------------------------

_GOOD_TEMPLATE = (
    "Context:\n{context_json}\n\n"
    "Reviewer:\n{reviewer_input}\n\n"
    "Return JSON."
)


def test_formalizer_high_confidence_is_committable():
    backend = FakeBackend([
        '```json\n{"note": {"x": 1}, "confidence": "high", '
        '"clarifying_questions": []}\n```'
    ])
    f = Formalizer(backend, _GOOD_TEMPLATE)
    result = f.formalize(
        context={"cell": "BABA:cloud:2023Q1"},
        reviewer_input="fy23 excludes bytedance",
    )
    assert result.is_committable is True
    assert result.confidence == "high"
    assert result.parsed["note"] == {"x": 1}


def test_formalizer_low_confidence_not_committable():
    backend = FakeBackend([
        '{"note": {"x": 1}, "confidence": "low", "clarifying_questions": []}'
    ])
    f = Formalizer(backend, _GOOD_TEMPLATE)
    result = f.formalize(context={}, reviewer_input="vague")
    assert result.is_committable is False


def test_formalizer_with_questions_not_committable():
    backend = FakeBackend([
        '{"note": {}, "confidence": "high", '
        '"clarifying_questions": ["which metric?"]}'
    ])
    f = Formalizer(backend, _GOOD_TEMPLATE)
    result = f.formalize(context={}, reviewer_input="input")
    assert result.is_committable is False
    assert result.clarifying_questions == ["which metric?"]


def test_formalizer_backend_error_surfaces_as_result():
    class BadBackend:
        def extract(self, system, user, response_schema=None):
            raise RuntimeError("boom")

    f = Formalizer(BadBackend(), _GOOD_TEMPLATE)
    result = f.formalize(context={}, reviewer_input="x")
    assert result.error == "boom"
    assert result.is_committable is False


def test_formalizer_reports_missing_template_variable():
    backend = FakeBackend(['{"confidence": "high"}'])
    f = Formalizer(backend, "uses {unknown_var}")
    result = f.formalize(context={}, reviewer_input="x")
    assert result.error and "prompt template missing variable" in result.error


# ---- ReviewSession end-to-end -------------------------------------

def _good_reply():
    return json.dumps({
        "note": {"scope": {"ticker": "BABA"}, "guidance": "excludes X"},
        "linked_cells": ["BABA:cloud:2023Q1", "BABA:cloud:2023Q2"],
        "clarifying_questions": [],
        "confidence": "high",
    })


def test_session_commits_on_good_reply():
    anomaly = Anomaly(
        id="BABA:cloud:2023Q1",
        title="BABA cloud Q1",
        context={"value_usd": 3200, "quote": "…"},
    )
    backend = FakeBackend([_good_reply()])
    formalizer = Formalizer(backend, _GOOD_TEMPLATE)
    io = ScriptedIO(
        inputs=["fy23 onwards excludes bytedance"],
        commits=[True],
    )
    written: list[tuple[Artifact, str]] = []
    effect_calls: list[list[str]] = []
    checker_calls: list[list[str]] = []

    def write(art, run_id):
        written.append((art, run_id))

    def effect(ids):
        effect_calls.append(ids)
        return "re-extracted 2 cells"

    def checker(ids):
        checker_calls.append(ids)
        return (2, 2)

    def build_artifact(result, a):
        return Artifact(
            id="HN-X",
            data=result.parsed,
            affected_ids=result.parsed.get("linked_cells", [a.id]),
        )

    sess = ReviewSession(
        anomalies=[anomaly],
        formalizer=formalizer,
        write_artifact=write,
        effect=effect,
        checker=checker,
        build_artifact=build_artifact,
        callbacks=io.as_callbacks(),
    )
    outcomes = sess.run(run_id="RUN-1")
    assert len(outcomes) == 1
    o = outcomes[0]
    assert o.action == "committed"
    assert o.artifact_id == "HN-X"
    assert (o.passed_after, o.total_after) == (2, 2)
    assert written[0][1] == "RUN-1"
    assert effect_calls == [["BABA:cloud:2023Q1", "BABA:cloud:2023Q2"]]
    assert checker_calls == [["BABA:cloud:2023Q1", "BABA:cloud:2023Q2"]]


def test_session_skips_on_empty_input():
    anomaly = Anomaly(id="x", title="x", context={})
    backend = FakeBackend([])  # not called
    io = ScriptedIO(inputs=[""], commits=[])

    def never(*args, **kwargs):
        raise AssertionError("should not be called")

    sess = ReviewSession(
        anomalies=[anomaly],
        formalizer=Formalizer(backend, _GOOD_TEMPLATE),
        write_artifact=never,
        effect=never,
        checker=never,
        build_artifact=never,
        callbacks=io.as_callbacks(),
    )
    outcomes = sess.run(run_id="R")
    assert len(outcomes) == 1
    assert outcomes[0].action == "skipped"


def test_session_quit_stops_early():
    anomalies = [Anomaly(id=f"n{i}", title=f"n{i}", context={}) for i in range(3)]
    io = ScriptedIO(inputs=["quit"], commits=[])

    def never(*args, **kwargs):
        raise AssertionError("should not be called")

    sess = ReviewSession(
        anomalies=anomalies,
        formalizer=Formalizer(FakeBackend([]), _GOOD_TEMPLATE),
        write_artifact=never,
        effect=never,
        checker=never,
        build_artifact=never,
        callbacks=io.as_callbacks(),
    )
    outcomes = sess.run(run_id="R")
    assert len(outcomes) == 1
    assert outcomes[0].action == "quit"


def test_session_clarification_loop_then_commit():
    anomaly = Anomaly(id="x", title="x", context={})
    # First reply asks a question; second (after clarification) is good.
    backend = FakeBackend([
        json.dumps({
            "clarifying_questions": ["Which metric — cloud or total?"],
            "confidence": "medium",
        }),
        _good_reply(),
    ])
    io = ScriptedIO(
        inputs=["fy23 excludes bytedance", "cloud only"],
        commits=[True],
    )
    written: list = []

    def write(a, r):
        written.append(a)

    sess = ReviewSession(
        anomalies=[anomaly],
        formalizer=Formalizer(backend, _GOOD_TEMPLATE),
        write_artifact=write,
        effect=lambda ids: "ok",
        checker=lambda ids: (2, 2),
        build_artifact=lambda r, a: Artifact(
            id="HN-Y", data=r.parsed,
            affected_ids=r.parsed.get("linked_cells", []),
        ),
        callbacks=io.as_callbacks(),
    )
    outcomes = sess.run(run_id="R")
    assert outcomes[0].action == "committed"
    assert len(backend.calls) == 2  # clarification happened
    assert len(written) == 1


def test_session_skips_when_reviewer_declines_commit():
    anomaly = Anomaly(id="x", title="x", context={})
    backend = FakeBackend([_good_reply()])
    io = ScriptedIO(inputs=["good input"], commits=[False])

    def boom(*args, **kwargs):
        raise AssertionError("write should not happen")

    sess = ReviewSession(
        anomalies=[anomaly],
        formalizer=Formalizer(backend, _GOOD_TEMPLATE),
        write_artifact=boom,
        effect=boom,
        checker=boom,
        build_artifact=lambda r, a: Artifact(
            id="x", data={}, affected_ids=[]),
        callbacks=io.as_callbacks(),
    )
    outcomes = sess.run(run_id="R")
    assert outcomes[0].action == "skipped"
    assert "declined" in outcomes[0].notes
