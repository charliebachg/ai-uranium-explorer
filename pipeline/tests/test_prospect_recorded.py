"""The recorded conversation the walkthrough replays, and the switch that picks which backend answers.

A recording is shown to an audience, so the two things worth pinning are that it keeps what the gate refused
— a withheld answer is the most informative thing in the demo, not an embarrassment to drop — and that it
carries only the values its published claims actually cite, since anything else would be dead weight in a file
the browser loads.
"""

from __future__ import annotations

import json

import pytest

from uranium_explorer.prospect import recorded as R
from uranium_explorer.prospect import serve as S
from uranium_explorer.prospect import tools as T


def val(vid: str, value, fmt: str = "m1", unit: str | None = None) -> dict:
    return {"id": vid, "kind": "stat", "as_printed": None, "value": value, "unit_as_printed": None,
            "fmt": fmt, "unit": unit}


VALUES = {
    "c:cell:x:d_conductor_m": val("c:cell:x:d_conductor_m", 820.0, unit="m"),
    "c:cell:x:holes_n": val("c:cell:x:holes_n", 493, fmt="int"),
}


class FakeBackend:
    """Answers each call with the next scripted step, so a recording can be made without a model. Since
    Phase 4c every turn is routed first; the router's call is answered `other` here so the scripted steps
    stay the loop's, as they were before the interface agent existed."""

    def __init__(self, steps):
        self.steps = list(steps)

    def call(self, req):
        step = {"kind": "other"} if req.task == "interface_route" else self.steps.pop(0)

        class Response:
            structured = step
            cost_usd = 0.011

        return Response()


@pytest.fixture
def stub_tools(monkeypatch):
    def fake(tool, args):
        return T.ToolResult(tool, args, rows=[{"feature": "d_conductor_m", "value": 820.0}], values=VALUES)

    monkeypatch.setattr(T, "call", fake)


def answer(text: str, claims: list[dict]) -> dict:
    return {"action": "answer", "answer": {"text": text, "claims": claims}}


def good() -> dict:
    return answer(
        "The nearest mapped conductor is 820.0 m away.",
        [{"text": "The nearest mapped conductor is 820.0 m away.",
          "value_ids": ["c:cell:x:d_conductor_m"]}],
    )


def fabricated() -> dict:
    return answer(
        "The nearest mapped conductor is 512.7 m away.",
        [{"text": "The nearest mapped conductor is 512.7 m away.",
          "value_ids": ["c:cell:x:d_conductor_m"]}],
    )


# ---------------------------------------------------------------- the recording


def test_a_recording_keeps_the_answer_the_gate_refused(stub_tools) -> None:
    """The withheld turn is the point of showing this at all: it is the check firing, in public. The gate
    hands the model its objections once before withholding, so a turn that stays fabricated takes two
    scripted replies."""
    payload = R.record("0001_0001", FakeBackend([fabricated(), fabricated(), good()]),
                       questions=("how far?", "how far again?"))
    assert [t["published"] for t in payload["turns"]] == [False, True]
    withheld = payload["turns"][0]
    assert withheld["text"] is None, "a refused answer is never carried in the file"
    assert any("512.7" in p for p in withheld["problems"]), "the objection travels with it"


def test_only_the_values_the_published_claims_cite_are_written(stub_tools) -> None:
    payload = R.record("0001_0001", FakeBackend([good()]), questions=("how far?",))
    assert set(payload["values"]) == {"c:cell:x:d_conductor_m"}
    assert "c:cell:x:holes_n" not in payload["values"], "a value nothing cites is dead weight in the browser"


def test_the_recording_says_when_and_by_what_it_was_made(stub_tools) -> None:
    payload = R.record("0001_0001", FakeBackend([good()]), model="gpt-5-mini", questions=("how far?",))
    assert payload["model"] == "gpt-5-mini"
    assert payload["cell_id"] == "0001_0001"
    assert payload["recorded_at"].endswith("+00:00") or "T" in payload["recorded_at"]
    assert payload["cost_usd"] > 0


def test_it_is_written_as_json_the_web_contract_can_read(stub_tools, tmp_path) -> None:
    payload = R.record("0001_0001", FakeBackend([good()]), questions=("how far?",))
    path = R.write(payload, out=tmp_path / "recorded_chat.json")
    back = json.loads(path.read_text())
    assert back["turns"][0]["claims"][0]["value_ids"] == ["c:cell:x:d_conductor_m"]


# ---------------------------------------------------------------- which backend answers


def test_the_chat_backend_can_be_chosen_and_the_two_never_share_a_cache() -> None:
    openai, openai_model = S.make_backend("openai", "")
    claude, claude_model = S.make_backend("claude", "")
    assert openai_model != "claude-sonnet-5"
    assert claude_model == "claude-sonnet-5"
    # the family is what keeps one backend's answer from being served for the other
    assert openai.inner.family != claude.inner.family


def test_an_unknown_backend_is_refused_rather_than_guessed_at() -> None:
    with pytest.raises(ValueError, match="openai, claude or auto"):
        S.make_backend("gemini", "")


def test_an_explicit_model_wins_over_the_default() -> None:
    _backend, model = S.make_backend("openai", "gpt-4.1-mini")
    assert model == "gpt-4.1-mini"
