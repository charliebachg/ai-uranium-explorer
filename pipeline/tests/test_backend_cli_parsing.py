import json
from pathlib import Path

import pytest

from uranium_explorer.backends import claude_cli as cc
from uranium_explorer.backends.base import ExtractionRequest, SchemaInvalidError, TransientBackendError

FIXTURE = Path(__file__).parent / "fixtures" / "replay" / "probe_drilllog_p1.json"


@pytest.fixture
def envelope():
    return json.loads(FIXTURE.read_text())["envelope"]


def test_structured_output_preferred(envelope):
    s = cc.structured_from_envelope(envelope)
    assert s["coordinates"]["kind"] == "local_grid"
    assert s["coordinates"]["datum_as_printed"] is None


def test_structured_falls_back_to_fenced_result():
    env = {"result": "```json\n{\"a\": 1}\n```"}
    assert cc.structured_from_envelope(env) == {"a": 1}


def test_structured_missing_raises():
    with pytest.raises(SchemaInvalidError):
        cc.structured_from_envelope({"result": "no json here"})


def test_resolve_model_ignores_side_calls(envelope):
    # The probe envelope contains a small Haiku side call alongside the requested model.
    assert cc.resolve_model(envelope, "claude-sonnet-5") == "claude-sonnet-5"
    assert cc.resolve_model(envelope, "some-other-id") == "claude-sonnet-5"


@pytest.mark.parametrize("text,resets", [
    ("You've hit your session limit · resets 6:40pm (Asia/Jakarta)", "6:40pm (Asia/Jakarta)"),
    ("Usage limit reached. Try again later.", None),
])
def test_usage_limit_detection(text, resets):
    err = cc.detect_usage_limit("", text)
    assert err is not None and err.resets_at_text == resets


def test_no_false_usage_limit():
    assert cc.detect_usage_limit('{"result": "the limit of detection is 0.01%"}') is None


def test_parse_envelope_handles_noise():
    assert cc.parse_envelope('warning: something\n{"type": "result"}')["type"] == "result"
    with pytest.raises(TransientBackendError):
        cc.parse_envelope("")


def test_argv_contract(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"png")
    req = ExtractionRequest(task="probe", images=(img,), system_prompt="SYS", user_prompt="read {STAGE_DIR}/page.png",
                            schema={"type": "object"}, schema_version="s1", prompt_version="p1",
                            model="claude-sonnet-5", effort="medium")
    argv = cc.build_argv(req, tmp_path / "stage", binary="claude")
    assert argv[:2] == ["claude", "-p"]
    assert argv[2] == f"read {tmp_path / 'stage'}/page.png"
    for flag in ("--restricted", "--no-session-persistence", "--strict-mcp-config", "--disable-slash-commands"):
        assert flag in argv
    assert "--bare" not in argv
    assert argv[argv.index("--tools") + 1] == "Read"
    assert argv[argv.index("--permission-prompts") + 1] == "none"
    assert argv[argv.index("--setting-sources") + 1] == ""


def test_cache_key_ignores_stage_dir_and_tracks_inputs(tmp_path):
    img = tmp_path / "x.png"
    img.write_bytes(b"png")
    base = dict(task="probe", images=(img,), system_prompt="S", user_prompt="read {STAGE_DIR}/page.png",
                schema={}, schema_version="s1", prompt_version="p1", model="m", effort="medium")
    k = ExtractionRequest(**base).cache_key("claude_cli")
    assert k == ExtractionRequest(**base).cache_key("claude_cli")
    assert k != ExtractionRequest(**{**base, "effort": "high"}).cache_key("claude_cli")
    assert k != ExtractionRequest(**{**base, "prompt_version": "p2"}).cache_key("claude_cli")
    img.write_bytes(b"png2")
    assert k != ExtractionRequest(**base).cache_key("claude_cli")


def test_child_env_strips_api_keys(monkeypatch):
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    monkeypatch.setenv("ANTHROPIC_AUTH_TOKEN", "tok")
    env = cc.child_env()
    assert "ANTHROPIC_API_KEY" not in env and "ANTHROPIC_AUTH_TOKEN" not in env
    assert env["DISABLE_AUTOUPDATER"] == "1"
