"""The auto backend is API first: every route is a pay-per-token API, a bare claude-* id goes to OpenRouter's
Anthropic listing, nothing depends on a tool on the host, and a model with no API route is refused with the fix."""

from __future__ import annotations

from dataclasses import replace
from types import SimpleNamespace

import pytest

from uranium_explorer.backends.base import BackendConfigError, ExtractionRequest
from uranium_explorer.backends.router import RoutedBackend, anthropic_listing, api_first, is_claude_model, is_openai_model


def _req(model: str) -> ExtractionRequest:
    return ExtractionRequest(task="t", model=model, system_prompt="s", user_prompt="u", schema={}, prompt_version="v",
                             effort="low", images=(), schema_version="1.0.0")


class Echo:
    family = "echo"

    def __init__(self, name: str) -> None:
        self.name, self.calls = name, []

    def call(self, req):
        self.calls.append(req.model)
        return SimpleNamespace(structured={"by": self.name, "model": req.model})


def test_bare_claude_ids_are_renamed_to_the_anthropic_listing_and_only_those() -> None:
    assert is_claude_model("claude-sonnet-5") and not is_claude_model("anthropic/claude-sonnet-5")
    assert anthropic_listing("claude-opus-5") == "anthropic/claude-opus-5"
    assert anthropic_listing("z-ai/glm-5.3-flash") == "z-ai/glm-5.3-flash"
    assert is_openai_model("gpt-5-mini") and not is_openai_model("openai/gpt-5-mini")


def test_routes_build_their_adapter_on_first_use_and_the_call_carries_the_renamed_model() -> None:
    built = []
    routed = RoutedBackend([(is_claude_model, lambda: built.append("a") or Echo("anthropic"))], rename=anthropic_listing)
    assert built == [], "nothing is built at construction"
    out = routed.call(_req("claude-sonnet-5"))
    assert built == ["a"] and out.structured == {"by": "anthropic", "model": "anthropic/claude-sonnet-5"}
    assert routed.route(_req("claude-sonnet-5")) is routed.route(_req("claude-opus-5")), "one adapter, built once"


def test_a_model_with_no_api_route_is_refused_with_the_fix_and_the_cli_is_never_a_route() -> None:
    routed = api_first(timeout_s=1)
    with pytest.raises(BackendConfigError, match="no API route for model 'sonnet'"):
        routed.route(_req("sonnet"))
    assert all(accepts.__name__ != "is_cli" for accepts, _ in routed.routes)


def test_api_first_builds_without_any_key_and_names_the_key_only_when_a_call_needs_it(monkeypatch) -> None:
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    monkeypatch.setattr("uranium_explorer.backends.openai_api.load_dotenv", lambda *a, **k: {})
    monkeypatch.setattr("uranium_explorer.backends.openrouter.load_dotenv", lambda *a, **k: {})   # imported by name there
    routed = api_first(timeout_s=1)
    adapter = routed.route(_req("z-ai/glm-5.3-flash"))
    assert adapter.family == "openrouter"
    assert routed.route(_req("claude-sonnet-5")).family == "openrouter"
    with pytest.raises(BackendConfigError, match="OPENROUTER_API_KEY"):
        routed.call(_req("z-ai/glm-5.3-flash"))


def test_an_adapter_given_directly_still_works_for_the_existing_callers() -> None:
    echo = Echo("given")
    routed = RoutedBackend([(lambda m: "/" in m, echo)], default=Echo("fallback"))
    assert routed.call(_req("v/m")).structured["by"] == "given"
    assert routed.call(_req("other")).structured["by"] == "fallback"
    assert replace(_req("v/m"), model="x").model == "x"
