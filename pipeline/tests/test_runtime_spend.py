"""One ledger, two ceilings and a run budget, all checked before a call leaves the machine."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacy_reader.backends import spend as shim
from legacy_reader.backends.base import ExtractionRequest, ExtractionResponse
from legacy_reader.backends import cache as C
from legacy_reader.backends.cache import CachedBackend
from legacy_reader.runtime import spend as S
from legacy_reader.runtime.spend import BudgetExhausted, RunBudget


@pytest.fixture
def caps(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("LR_MAX_SPEND_USD", "1.00")
    monkeypatch.setenv("OPENAI_MAX_SPEND_USD", "0.50")


def lines(path: Path | None = None) -> list[dict]:
    p = path or S.ledger_path()
    return [json.loads(ln) for ln in p.read_text().splitlines()] if p.exists() else []


# ---------------------------------------------------------------- the ledger


def test_record_writes_one_line_per_call_with_its_family(caps) -> None:
    S.record("claude_cli", "claude-opus-5", 0.29, 15000, 4000, task="extract_page")
    S.record("openai", "gpt-5-mini", 0.001, 1000, 100, task="prospect_chat", price=S.Price(0.25, 2.0))
    rows = lines()
    assert [r["family"] for r in rows] == ["claude_cli", "openai"]
    assert rows[0] == {**rows[0], "model": "claude-opus-5", "task": "extract_page", "tokens_in": 15000,
                       "tokens_out": 4000, "usd": 0.29}
    assert rows[1]["per_mtok_in"] == 0.25 and rows[1]["per_mtok_out"] == 2.0
    assert S.spent_usd() == pytest.approx(0.291)
    assert S.spent_usd("claude_cli") == pytest.approx(0.29)
    assert S.spent_usd("openai") == pytest.approx(0.001)
    assert S.by_family() == pytest.approx({"claude_cli": 0.29, "openai": 0.001})


def test_the_caps_come_from_the_environment_with_stated_defaults(monkeypatch) -> None:
    monkeypatch.delenv("OPENAI_MAX_SPEND_USD", raising=False)
    assert S.cap_usd() == 300.0 and S.cap_usd("openai") == 2.00
    assert S.cap_usd("claude_cli") == 300.0, "a family without a ceiling of its own is bounded by the total"
    monkeypatch.setenv("LR_MAX_SPEND_USD", "12.5")
    assert S.cap_usd() == 12.5 == S.cap_usd("claude_cli")


def test_check_passes_when_nothing_would_be_crossed(caps) -> None:
    S.check("claude_cli", 0.5)
    S.check("openai", 0.5, RunBudget(cap_usd=0.5, spent_usd=0.0))
    S.check("claude_cli", 0.5, RunBudget(cap_usd=None, spent_usd=99.0))   # an uncapped run


def test_check_refuses_when_the_run_budget_would_be_crossed(caps) -> None:
    budget = RunBudget(cap_usd=1.0, spent_usd=0.75)
    with pytest.raises(BudgetExhausted, match="budget"):
        S.check("claude_cli", 0.5, budget)
    S.check("claude_cli", 0.25, budget)   # exactly reaching it is allowed (binary-exact figures on purpose)


def test_check_refuses_when_the_total_ceiling_would_be_crossed(caps) -> None:
    S.record("claude_cli", "m", 0.9)
    with pytest.raises(BudgetExhausted, match="LR_MAX_SPEND_USD"):
        S.check("claude_cli", 0.2)
    S.check("claude_cli", 0.1)


def test_check_refuses_when_a_family_ceiling_would_be_crossed(caps) -> None:
    S.record("openai", "m", 0.45)
    with pytest.raises(BudgetExhausted, match="OPENAI_MAX_SPEND_USD"):
        S.check("openai", 0.1)
    S.check("claude_cli", 0.1)   # the OpenAI ceiling is not the CLI family's


def test_an_unreadable_line_stops_every_family_rather_than_counting_as_free(caps) -> None:
    S.ledger_path().parent.mkdir(parents=True, exist_ok=True)
    S.ledger_path().write_text('{"family": "openai", "usd": 0.1}\nnot json\n')
    with pytest.raises(BudgetExhausted, match="cannot read"):
        S.check("claude_cli")


def test_the_old_openai_ledger_is_imported_on_first_read(caps) -> None:
    old = S.legacy_ledger_path()
    old.parent.mkdir(parents=True, exist_ok=True)
    old.write_text('{"at": "t", "model": "gpt-5-mini", "usd": 0.2}\n{"at": "t", "model": "gpt-5-mini", "usd": 0.3}\n')
    assert S.spent_usd("openai") == pytest.approx(0.5)
    assert [r["family"] for r in lines()] == ["openai", "openai"], "imported lines carry their family"
    assert old.exists(), "the old file is history, not deleted"
    S.record("openai", "gpt-5-mini", 0.1)
    assert S.spent_usd("openai") == pytest.approx(0.6), "a second read does not import twice"


def test_the_shim_is_the_openai_family_of_the_same_ledger(caps) -> None:
    shim.record("gpt-5-mini", 1_000_000, 0, S.Price(0.25, 2.0), task="prospect_chat")
    assert shim.spent_usd() == pytest.approx(0.25) == S.spent_usd("openai")
    assert shim.cap_usd() == 0.5 and shim.remaining_usd() == pytest.approx(0.25)
    assert shim.ledger_path() == S.ledger_path()
    assert shim.BudgetExhausted is BudgetExhausted
    S.record("claude_cli", "m", 0.4)
    with pytest.raises(BudgetExhausted, match="ceiling"):
        shim.check(0.4)   # the total ceiling applies to the OpenAI family too


# ---------------------------------------------------------------- the cached backend


class ScriptedBackend:
    family = "claude_cli"

    def __init__(self, cost: float = 0.05):
        self.cost = cost
        self.calls = 0

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        self.calls += 1
        return ExtractionResponse(structured={"ok": True}, envelope={}, backend="scripted", backend_version="0",
                                  model_requested=req.model, model_resolved=req.model, num_turns=4,
                                  duration_s=0.01, usage={"output_tokens": 100, "cache_creation_input_tokens": 7000,
                                                          "cache_read_input_tokens": 3000},
                                  cost_usd=self.cost, cache_key=req.cache_key(self.family))


def request(tmp_path: Path) -> ExtractionRequest:
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG" + b"x" * 32)
    return ExtractionRequest(task="extract_page", images=(img,), system_prompt="s", user_prompt="u",
                             schema={"type": "object"}, schema_version="1", prompt_version="1",
                             model="claude-sonnet-5", effort="medium")


def test_a_live_call_is_checked_and_charged_and_a_cache_hit_is_neither(tmp_path, caps, monkeypatch) -> None:
    checks: list[tuple] = []
    real_check = C.check_budget
    monkeypatch.setattr(C, "check_budget", lambda *a, **k: checks.append(a) or real_check(*a, **k))
    inner = ScriptedBackend()
    budget = RunBudget(cap_usd=1.0)
    backend = CachedBackend(inner, root=tmp_path / "cache", run_budget=budget, estimate_usd=0.5)

    first = backend.call(request(tmp_path))
    assert first.from_cache is False and inner.calls == 1
    assert checks == [("claude_cli", 0.5, budget)]
    rows = lines()
    assert len(rows) == 1 and rows[0]["family"] == "claude_cli" and rows[0]["usd"] == 0.05
    assert rows[0]["tokens_in"] == 10000 and rows[0]["tokens_out"] == 100 and rows[0]["task"] == "extract_page"
    assert budget.spent_usd == pytest.approx(0.05)

    second = backend.call(request(tmp_path))
    assert second.from_cache is True and inner.calls == 1
    assert len(checks) == 1, "a cache hit is never checked"
    assert len(lines()) == 1 and budget.spent_usd == pytest.approx(0.05), "a cache hit is never charged"


def test_an_exhausted_run_budget_refuses_before_the_backend_is_called(tmp_path, caps) -> None:
    inner = ScriptedBackend()
    backend = CachedBackend(inner, root=tmp_path / "cache", run_budget=RunBudget(cap_usd=0.4), estimate_usd=0.5)
    with pytest.raises(BudgetExhausted):
        backend.call(request(tmp_path))
    assert inner.calls == 0 and lines() == []
    assert not (tmp_path / "cache" / "failures").exists(), "a refused call is not a backend failure"


def test_the_cumulative_ceiling_applies_to_the_cli_family_too(tmp_path, caps) -> None:
    S.record("claude_cli", "m", 0.95)
    inner = ScriptedBackend()
    with pytest.raises(BudgetExhausted, match="LR_MAX_SPEND_USD"):
        CachedBackend(inner, root=tmp_path / "cache", estimate_usd=0.1).call(request(tmp_path))
    assert inner.calls == 0


def test_a_backend_that_settles_the_ledger_itself_is_not_charged_twice(tmp_path, caps) -> None:
    class SelfRecording(ScriptedBackend):
        records_spend = True

    budget = RunBudget(cap_usd=1.0)
    CachedBackend(SelfRecording(), root=tmp_path / "cache", run_budget=budget).call(request(tmp_path))
    assert lines() == [], "the wrapper writes no line for a backend that wrote its own"
    assert budget.spent_usd == pytest.approx(0.05), "but the run budget still sees the cost"
