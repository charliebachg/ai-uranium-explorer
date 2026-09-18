"""The conversation: same tools, same evidence record, same gate as a published memo."""

from __future__ import annotations

import json

import pytest

from legacy_reader.prospect import chat as C
from legacy_reader.prospect import tools as T


def val(vid: str, value, fmt: str = "m1", unit: str | None = None) -> dict:
    return {"id": vid, "kind": "stat", "as_printed": None, "value": value, "unit_as_printed": None,
            "fmt": fmt, "unit": unit}


class FakeBackend:
    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def call(self, req):
        self.requests.append(req)
        step = self.steps.pop(0)

        class R:
            structured = step
            cost_usd = 0.02

        return R()


@pytest.fixture
def stub_tools(monkeypatch):
    def fake(tool, args):
        return T.ToolResult(tool, args, rows=[{"feature": "d_conductor_m", "value": 820.0}],
                            values={"c:cell:x:d_conductor_m": val("c:cell:x:d_conductor_m", 820.0, unit="m")})

    monkeypatch.setattr(T, "call", fake)


def answer(text: str, claims: list[dict]) -> dict:
    return {"action": "answer", "answer": {"text": text, "claims": claims}}


def test_a_grounded_answer_is_returned(stub_tools):
    conv = C.Conversation(cell_id="0001_0001")
    backend = FakeBackend([answer(
        "The nearest mapped conductor is 820.0 m away.",
        [{"text": "The nearest mapped conductor is 820.0 m away.",
          "value_ids": ["c:cell:x:d_conductor_m"]}])])
    turn = C.ask(conv, "how close is the nearest conductor?", backend)
    assert turn["published"] is True
    assert "820.0" in turn["text"]
    assert turn["cost_usd"] > 0


def test_an_answer_with_a_number_from_nowhere_is_withheld(stub_tools):
    conv = C.Conversation(cell_id="0001_0001")
    backend = FakeBackend([answer(
        "Grades of 4.7% U3O8 were intersected nearby.",
        [{"text": "Grades of 4.7% U3O8 were intersected nearby.", "value_ids": []}])])
    turn = C.ask(conv, "any grades?", backend)
    assert turn["published"] is False
    assert turn["text"] is None, "an answer that fails the check is not shown at all"
    assert any("4.7" in p for p in turn["problems"])


def test_prose_is_checked_as_well_as_the_claims(stub_tools):
    """A number smuggled into the summary rather than a claim must still be caught."""
    conv = C.Conversation(cell_id="0001_0001")
    backend = FakeBackend([answer(
        "The conductor is 820.0 m away and the unconformity sits at 512.7 m.",
        [{"text": "The conductor is 820.0 m away.", "value_ids": ["c:cell:x:d_conductor_m"]}])])
    turn = C.ask(conv, "tell me about it", backend)
    assert turn["published"] is False
    assert any("512.7" in p for p in turn["problems"])


def test_the_agent_may_call_a_tool_before_answering(stub_tools):
    conv = C.Conversation(cell_id="0001_0001")
    backend = FakeBackend([
        {"action": "call_tool", "tool": "retrieve", "args": {"query": "conductor", "cell_id": "0001_0001"}},
        answer("Nothing numeric in this answer.", [{"text": "Nothing numeric.", "value_ids": []}]),
    ])
    turn = C.ask(conv, "what does the record say?", backend)
    assert turn["published"] is True
    assert "retrieve" in turn["tools_used"]


def test_the_evidence_record_is_opened_once_and_reused(stub_tools):
    conv = C.Conversation(cell_id="0001_0001")
    backend = FakeBackend([
        answer("One.", [{"text": "One.", "value_ids": []}]),
        answer("Two.", [{"text": "Two.", "value_ids": []}]),
    ])
    C.ask(conv, "first?", backend)
    opened = len(conv.calls)
    C.ask(conv, "second?", backend)
    assert len(conv.calls) == opened, "a second question reuses the record rather than rebuilding it"
    assert len(conv.turns) == 2


def test_the_transcript_carries_earlier_turns_into_the_prompt(stub_tools):
    conv = C.Conversation(cell_id="0001_0001")
    backend = FakeBackend([
        answer("The host is mapped here.", [{"text": "The host is mapped here.", "value_ids": []}]),
        answer("Still nothing numeric.", [{"text": "Still.", "value_ids": []}]),
    ])
    C.ask(conv, "is there a host?", backend)
    C.ask(conv, "and what about the trap?", backend)
    second_prompt = backend.requests[-1].user_prompt
    assert "is there a host?" in second_prompt
    assert "The host is mapped here." in second_prompt


def test_running_out_of_steps_is_reported_rather_than_guessed(stub_tools):
    conv = C.Conversation(cell_id="0001_0001")
    backend = FakeBackend([{"action": "call_tool", "tool": "coverage", "args": {}}] * C.MAX_STEPS)
    turn = C.ask(conv, "well?", backend)
    assert turn["published"] is False
    assert "ran out of steps" in turn["problems"][0]


def test_the_system_prompt_states_the_rules_that_matter():
    low = C.SYSTEM.lower()
    for phrase in ("every number you state must come from a tool result",
                   "never compute anything yourself",
                   "unknown is not absent",
                   "never recommend drilling"):
        assert phrase in low, phrase


def test_the_agent_is_told_it_may_refuse():
    assert "cannot be answered" in C.SYSTEM


def test_two_questions_do_not_share_a_cache_key(stub_tools):
    """The staged evidence is identical between turns; only the question differs, and it must decide the key.

    Without this, the second question is answered from the first one's cached response: a stale answer that
    reads perfectly and is about the wrong thing.
    """
    conv = C.Conversation(cell_id="0001_0001")
    backend = FakeBackend([
        answer("First.", [{"text": "First.", "value_ids": []}]),
        answer("Second.", [{"text": "Second.", "value_ids": []}]),
    ])
    C.ask(conv, "how close is the conductor?", backend)
    C.ask(conv, "has anyone drilled here?", backend)
    keys = [r.cache_key("claude_cli") for r in backend.requests]
    assert keys[0] != keys[1]
