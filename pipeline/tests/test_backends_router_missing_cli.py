"""The auto route on a host without the `claude` binary: the service and the analyst arms still build, and only a
request that would need the CLI is refused, at call time, with the fix named."""

from __future__ import annotations

import shutil
from types import SimpleNamespace

import pytest

from uranium_explorer.backends.base import BackendConfigError
from uranium_explorer.backends.router import MissingCliBackend, cli_or_missing


def test_without_the_binary_the_stand_in_is_chosen_and_refuses_with_the_fix(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    made = []
    backend = cli_or_missing(lambda: made.append(1) or "cli")
    assert isinstance(backend, MissingCliBackend) and made == [], "the CLI adapter is never constructed"
    with pytest.raises(BackendConfigError, match="does not have"):
        backend.call(SimpleNamespace(model="claude-sonnet-5"))


def test_with_the_binary_the_real_adapter_is_built(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: "/usr/local/bin/claude")
    assert cli_or_missing(lambda: "cli") == "cli"


def test_the_analyst_arm_factory_builds_without_the_binary(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda _name: None)
    monkeypatch.setenv("OPENROUTER_API_KEY", "test-key-never-used")
    from uranium_explorer.analyst import cli as ACLI

    factory = ACLI._factory("auto")
    arm = SimpleNamespace(timeout_s=60, max_budget_usd_per_call=0.1)
    routed = factory(arm)
    assert isinstance(routed.default, MissingCliBackend)
    assert routed.route(SimpleNamespace(model="z-ai/glm-5.3-flash")).family != "claude_cli"
