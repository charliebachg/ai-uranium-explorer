"""The environment check tells a stranger what is missing without failing on what is optional."""

from __future__ import annotations

import shutil

from uranium_explorer import doctor as D


def test_without_the_cli_or_a_key_nothing_required_fails(monkeypatch) -> None:
    monkeypatch.setattr(shutil, "which", lambda name: None if name == "claude" else f"/usr/bin/{name}")
    monkeypatch.setattr("uranium_explorer.backends.openai_api.load_dotenv", lambda *a, **k: {})
    monkeypatch.delenv("OPENROUTER_API_KEY", raising=False)
    checks = {c.name: c for c in D.run_checks()}
    cli, key = checks["claude CLI (optional)"], checks["OPENROUTER_API_KEY"]
    assert cli.ok is False and cli.required is False, "the CLI is a developer's option, never a failure"
    assert key.ok is False and key.required is False and ".env" in key.detail
    assert not [c for c in checks.values() if c.required and not c.ok and c.name in ("claude CLI (optional)", "OPENROUTER_API_KEY")]


def test_a_present_key_is_reported_by_name_only(monkeypatch) -> None:
    monkeypatch.setattr("uranium_explorer.backends.openai_api.load_dotenv", lambda *a, **k: {})
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-test-never-printed")
    checks = {c.name: c for c in D.run_checks()}
    assert checks["OPENROUTER_API_KEY"].ok is True and "sk-or" not in checks["OPENROUTER_API_KEY"].detail
