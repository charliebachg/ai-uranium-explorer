"""The OpenRouter adapter: images travel as parts, the schema goes with the ask, the provider's own cost lands
on its ledger family, the key never lands anywhere, and a routed arm keys its cache by the adapter that
answered."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from legacy_reader.backends import cache as C
from legacy_reader.backends import openrouter as OR
from legacy_reader.backends.base import BackendConfigError, ExtractionRequest, SchemaInvalidError, TransientBackendError
from legacy_reader.backends.router import RoutedBackend
from legacy_reader.runtime import spend as S

SCHEMA = {"type": "object", "properties": {"status": {"type": "string"}}, "required": ["status"]}


class FakeHttp:
    """Answers every POST from a queue of (status, body) and records what was sent."""

    def __init__(self, replies: list[tuple[int, Any]], models: list[dict[str, Any]] | None = None) -> None:
        self.replies = list(replies)
        self.sent: list[dict[str, Any]] = []
        self.models = models or []

    def post(self, url: str, headers: dict[str, str], json: dict[str, Any], timeout: float) -> Any:
        self.sent.append({"url": url, "headers": headers, "json": json})
        status, body = self.replies.pop(0)
        text = body if isinstance(body, str) else __import__("json").dumps(body)
        return SimpleNamespace(status_code=status, text=text, json=lambda: body)

    def get(self, url: str, timeout: float) -> Any:
        return SimpleNamespace(status_code=200, text="", json=lambda: {"data": self.models})


def reply(structured: dict[str, Any], cost: float | None = 0.0021, model: str = "z-ai/glm-5.3-flash") -> dict[str, Any]:
    usage = {"prompt_tokens": 1200, "completion_tokens": 80}
    if cost is not None:
        usage["cost"] = cost
    return {"id": "gen-1", "model": model, "provider": "Z.ai", "usage": usage,
            "choices": [{"finish_reason": "stop", "message": {"content": json.dumps(structured)}}]}


@pytest.fixture
def env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    ledger = tmp_path / "spend.jsonl"
    monkeypatch.setattr(S, "ledger_path", lambda: ledger)
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-not-a-real-key")
    monkeypatch.setenv("OPENROUTER_MAX_SPEND_USD", "1.00")
    return ledger


def request(tmp_path: Path, model: str = "z-ai/glm-5.3-flash", with_image: bool = True) -> ExtractionRequest:
    card = tmp_path / "card.png"
    card.write_bytes(b"\x89PNG\r\n\x1a\nfakecard")
    staged = tmp_path / "tool_01_cell_features.json"
    staged.write_text('{"rows": [{"feature": "d_conductor_m", "value": 820.0}]}')
    return ExtractionRequest(task="analyst_v1_execute", images=(card,) if with_image else (), stage_files=((staged, staged.name),),
                             system_prompt="You are the executor.", user_prompt="Segment s01: criterion conductor_proximity.\nRead {STAGE_DIR}/tool_01_cell_features.json",
                             schema=SCHEMA, schema_version="1.0.0", prompt_version="analyst/v1/v1", model=model, effort="medium")


def test_images_travel_as_parts_and_the_schema_goes_with_the_ask(tmp_path: Path, env: Path) -> None:
    http = FakeHttp([(200, reply({"status": "met"}))])
    resp = OR.OpenRouterBackend(http=http, prices={}).call(request(tmp_path))
    sent = http.sent[0]["json"]
    assert sent["model"] == "z-ai/glm-5.3-flash" and sent["usage"] == {"include": True}
    assert sent["response_format"]["type"] == "json_schema" and sent["response_format"]["json_schema"]["schema"] == SCHEMA
    user = sent["messages"][1]["content"]
    assert isinstance(user, list) and user[0]["type"] == "text" and user[1]["type"] == "image_url"
    assert user[1]["image_url"]["url"].startswith("data:image/png;base64,")
    assert "tool_01_cell_features.json" in user[0]["text"] and "820.0" in user[0]["text"], "staged files are inlined"
    assert "{STAGE_DIR}" not in user[0]["text"]
    assert "JSON Schema" in sent["messages"][0]["content"]
    assert resp.structured == {"status": "met"} and resp.backend == "openrouter" and resp.model_resolved == "z-ai/glm-5.3-flash"
    assert resp.cache_key == request(tmp_path).cache_key("openrouter")


def test_the_providers_cost_is_what_lands_on_the_ledger_and_the_key_lands_nowhere(tmp_path: Path, env: Path) -> None:
    http = FakeHttp([(200, reply({"status": "met"}, cost=0.0021))])
    resp = OR.OpenRouterBackend(http=http, prices={}).call(request(tmp_path))
    assert resp.cost_usd == pytest.approx(0.0021) and resp.envelope["cost_reported"] is True
    lines = [json.loads(l) for l in env.read_text().splitlines()]
    assert len(lines) == 1 and lines[0]["family"] == "openrouter" and lines[0]["usd"] == pytest.approx(0.0021)
    assert lines[0]["model"] == "z-ai/glm-5.3-flash" and lines[0]["tokens_in"] == 1200
    assert http.sent[0]["headers"]["Authorization"] == "Bearer sk-or-test-not-a-real-key"
    everything = json.dumps(lines) + json.dumps(resp.envelope) + json.dumps(resp.usage)
    assert "sk-or-test" not in everything
    assert S.spent_usd("openrouter") == pytest.approx(0.0021)


def test_without_a_reported_cost_the_published_price_is_used_and_failing_that_a_pessimistic_one(tmp_path: Path, env: Path) -> None:
    models = [{"id": "z-ai/glm-5.3-flash", "name": "GLM", "pricing": {"prompt": "0.00000009", "completion": "0.0000003"},
               "architecture": {"input_modalities": ["text", "image"]}, "supported_parameters": ["structured_outputs"], "context_length": 100}]
    http = FakeHttp([(200, reply({"status": "met"}, cost=None))], models=models)
    resp = OR.OpenRouterBackend(http=http).call(request(tmp_path))
    assert resp.cost_usd == pytest.approx(1200 * 0.09e-6 + 80 * 0.3e-6) and resp.envelope["cost_reported"] is False
    http = FakeHttp([(200, reply({"status": "met"}, cost=None, model="vendor/unknown"))], models=[])
    resp = OR.OpenRouterBackend(http=http).call(request(tmp_path, model="vendor/unknown"))
    assert resp.cost_usd == pytest.approx(OR.FALLBACK_PRICE.usd(1200, 80)), "an unlisted model is priced high, never free"


def test_a_provider_that_refuses_the_schema_is_asked_for_a_json_object_instead(tmp_path: Path, env: Path) -> None:
    http = FakeHttp([(400, {"error": {"message": "response_format json_schema is not supported by this model"}}),
                     (200, reply({"status": "unknown"}))])
    resp = OR.OpenRouterBackend(http=http, prices={}).call(request(tmp_path))
    assert resp.structured == {"status": "unknown"} and resp.envelope["structured_ask"] is False
    assert http.sent[1]["json"]["response_format"] == {"type": "json_object"}


def test_a_reply_that_is_not_json_is_retried_and_then_refused_charged_each_time(tmp_path: Path, env: Path) -> None:
    bad = {"id": "gen", "model": "z-ai/glm-5.3-flash", "usage": {"prompt_tokens": 10, "completion_tokens": 5, "cost": 0.001},
           "choices": [{"message": {"content": "I cannot answer in JSON."}}]}
    http = FakeHttp([(200, bad), (200, bad), (200, bad)])
    with pytest.raises(SchemaInvalidError):
        OR.OpenRouterBackend(http=http, prices={}).call(request(tmp_path))
    assert len(env.read_text().splitlines()) == 3, "every attempt that reached the provider is on the ledger"


def test_transient_and_configuration_failures_are_told_apart(tmp_path: Path, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(OR.time, "sleep", lambda s: None)
    http = FakeHttp([(503, "busy")] * (OR.RETRIES + 1))
    with pytest.raises(TransientBackendError, match="waits"):
        OR.OpenRouterBackend(http=http, prices={}).call(request(tmp_path))
    assert len(http.sent) == OR.RETRIES + 1, "a provider error is waited out before it is raised"
    http = FakeHttp([(429, "slow down"), (429, "slow down"), (200, reply({"status": "met"}))])
    assert OR.OpenRouterBackend(http=http, prices={}).call(request(tmp_path)).structured == {"status": "met"}
    with pytest.raises(BackendConfigError, match="not found"):
        OR.OpenRouterBackend(http=FakeHttp([(404, "no such model")]), prices={}).call(request(tmp_path))
    with pytest.raises(BackendConfigError, match="refused"):
        OR.OpenRouterBackend(http=FakeHttp([(401, "bad key")]), prices={}).call(request(tmp_path))
    with pytest.raises(BackendConfigError, match="not an OpenRouter model"):
        OR.OpenRouterBackend(http=FakeHttp([]), prices={}).call(request(tmp_path, model="claude-opus-5"))


def test_the_ceiling_is_checked_before_anything_is_sent(tmp_path: Path, env: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPENROUTER_MAX_SPEND_USD", "0.0001")
    http = FakeHttp([(200, reply({"status": "met"}))])
    with pytest.raises(S.BudgetExhausted):
        OR.OpenRouterBackend(http=http, prices={}).call(request(tmp_path))
    assert http.sent == []


def test_list_models_reads_prices_vision_and_schema_support(env: Path) -> None:
    models = [{"id": "qwen/qwen3.7-flash", "name": "Qwen", "pricing": {"prompt": "0.00000003", "completion": "0.00000013"},
               "architecture": {"input_modalities": ["text", "image"]}, "supported_parameters": ["response_format"], "context_length": 1000000},
              {"id": "deepseek/deepseek-v4-flash", "name": "DS", "pricing": {"prompt": "0.00000004", "completion": "0.00000007"},
               "architecture": {"input_modalities": ["text"]}, "supported_parameters": ["structured_outputs", "response_format"], "context_length": 1048576}]
    out = OR.list_models(http=FakeHttp([], models=models))
    assert [m["id"] for m in out] == ["deepseek/deepseek-v4-flash", "qwen/qwen3.7-flash"]
    q = out[1]
    assert q["vision"] is True and q["structured_outputs"] is False and q["usd_per_mtok_in"] == pytest.approx(0.03)
    assert out[0]["vision"] is False and out[0]["structured_outputs"] is True


# ---------------------------------------------------------------- the router under the cache


class CliLike:
    family = "claude_cli"

    def __init__(self) -> None:
        self.calls: list[str] = []

    def call(self, req: ExtractionRequest) -> Any:
        from legacy_reader.backends.base import ExtractionResponse

        self.calls.append(req.model)
        return ExtractionResponse(structured={"status": "met"}, envelope={}, backend="cli", backend_version="0",
                                  model_requested=req.model, model_resolved=req.model, num_turns=1, duration_s=0.1,
                                  usage={"input_tokens": 10, "output_tokens": 5}, cost_usd=0.5, cache_key=req.cache_key(self.family))


def test_a_routed_arm_keys_its_cache_and_its_ledger_by_the_adapter_that_answered(tmp_path: Path, env: Path) -> None:
    http = FakeHttp([(200, reply({"status": "met"}))])
    cli = CliLike()
    routed = RoutedBackend([(OR.is_openrouter_model, OR.OpenRouterBackend(http=http, prices={}))], default=cli)
    cached = C.CachedBackend(routed, root=tmp_path / "cache", run_budget=S.RunBudget(cap_usd=10.0, spent_usd=0.0), estimate_usd=0.1)
    cheap = request(tmp_path)
    opus = request(tmp_path, model="claude-opus-5")
    assert cached.key_for(cheap) == cheap.cache_key("openrouter") and cached.key_for(opus) == opus.cache_key("claude_cli")
    r1 = cached.call(cheap)
    r2 = cached.call(opus)
    assert r1.backend == "openrouter" and r2.backend == "cli" and cli.calls == ["claude-opus-5"]
    lines = [json.loads(l) for l in env.read_text().splitlines()]
    assert sorted(l["family"] for l in lines) == ["claude_cli", "openrouter"], "each adapter's line under its own family, once"
    assert cached.run_budget.spent_usd == pytest.approx(0.5 + 0.0021)
    again = cached.call(cheap)
    assert again.from_cache is True and len(http.sent) == 1, "the routed call is served from its own key"


def test_the_requests_effort_is_a_hard_reasoning_budget_and_a_cut_reply_is_asked_again_without_thinking(tmp_path: Path, env: Path) -> None:
    """These models think before they answer and the thinking is billed as completion tokens; an effort hint
    was not honoured (twelve-minute verifier calls at low and at medium alike), so the effort is sent as a
    token budget the provider must stop at, and a reply cut mid-thought even so is retried with no thinking."""
    cut = {"id": "gen", "model": "z-ai/glm-5.3-flash", "usage": {"prompt_tokens": 600, "completion_tokens": 7000, "cost": 0.001,
                                                                  "completion_tokens_details": {"reasoning_tokens": 4000}},
           "choices": [{"finish_reason": "length", "message": {"content": ""}}]}
    http = FakeHttp([(200, cut), (200, reply({"status": "met"}))])
    resp = OR.OpenRouterBackend(http=http, prices={}).call(request(tmp_path))
    first, second = http.sent[0]["json"], http.sent[1]["json"]
    assert first["reasoning"] == {"max_tokens": 4000} and first["max_tokens"] == 4000 + OR.ANSWER_TOKENS, "medium effort: a 4,000-token budget"
    assert second["reasoning"] == {"effort": "low"} and second["max_tokens"] == OR.RETRY_ROOM, \
        "the fallback is an effort hint alone (never both with a budget) with the room opened wide"
    assert resp.structured == {"status": "met"} and resp.envelope["reasoning"] == {"effort": "low"}
    assert len(env.read_text().splitlines()) == 2, "the cut attempt was charged too"
    http = FakeHttp([(200, reply({"status": "met"}))])
    OR.OpenRouterBackend(http=http, prices={}).call(request(tmp_path, model="qwen/qwen3.8-flash").__class__(**{**request(tmp_path).__dict__, "effort": "low"}))
    assert http.sent[0]["json"]["reasoning"] == {"max_tokens": 2000}
