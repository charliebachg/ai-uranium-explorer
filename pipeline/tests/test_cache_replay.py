"""Cache keys, failure isolation and strict replay."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacy_reader.backends.base import (
    ExtractionRequest,
    ExtractionResponse,
    ReplayMiss,
    TransientBackendError,
)
from legacy_reader.backends.cache import CachedBackend, failure_path, record_path
from legacy_reader.backends.replay import ReplayBackend

SCHEMA = {"type": "object", "additionalProperties": False, "properties": {}, "required": []}


def make_request(tmp_path: Path, **over) -> ExtractionRequest:
    img = over.pop("image", None)
    if img is None:
        img = tmp_path / "page.png"
        if not img.exists():
            img.write_bytes(b"\x89PNG\r\n\x1a\n" + b"a" * 64)
    base = dict(task="extract_page", images=(img,), system_prompt="sys", user_prompt="user",
                schema=SCHEMA, schema_version="s1", prompt_version="p1", model="claude-sonnet-5",
                effort="medium", context_hash="", render_params={"dpi": 200})
    base.update(over)
    return ExtractionRequest(**base)


class FakeBackend:
    family = "claude_cli"

    def __init__(self, structured=None, error=None):
        self.structured = structured or {"ok": True}
        self.error = error
        self.calls = 0

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        self.calls += 1
        if self.error:
            raise self.error
        return ExtractionResponse(structured=self.structured, envelope={"result": "x"}, backend="fake",
                                  backend_version="0", model_requested=req.model, model_resolved=req.model,
                                  num_turns=4, duration_s=1.0, usage={"output_tokens": 10}, cost_usd=0.05,
                                  cache_key=req.cache_key(self.family))


def test_cache_key_changes_with_every_input_that_changes_the_answer(tmp_path):
    base = make_request(tmp_path)
    k = base.cache_key("claude_cli")
    for field, value in (("model", "claude-opus-5"), ("effort", "high"), ("prompt_version", "p2"),
                         ("schema_version", "s2"), ("context_hash", "abc"), ("task", "triage"),
                         ("render_params", {"dpi": 300})):
        assert make_request(tmp_path, **{field: value}).cache_key("claude_cli") != k, field
    assert base.cache_key("anthropic_api") != k
    other = tmp_path / "other.png"
    other.write_bytes(b"different bytes")
    assert make_request(tmp_path, image=other).cache_key("claude_cli") != k


def test_cache_key_ignores_what_does_not_change_the_answer(tmp_path):
    a = make_request(tmp_path)
    b = make_request(tmp_path, system_prompt="a totally different system prompt text")
    assert a.cache_key("claude_cli") == b.cache_key("claude_cli"), \
        "the prompt version, not the prompt text, is what the key tracks"


def test_a_success_is_cached_and_served_without_a_second_call(tmp_path):
    inner = FakeBackend({"page_level": {}})
    cached = CachedBackend(inner, root=tmp_path / "cache")
    req = make_request(tmp_path)
    first = cached.call(req)
    assert first.from_cache is False and inner.calls == 1
    second = cached.call(req)
    assert second.from_cache is True and inner.calls == 1
    assert second.structured == {"page_level": {}}
    assert record_path(req.cache_key("claude_cli"), tmp_path / "cache").is_file()


def test_a_failure_is_recorded_separately_and_never_served_as_a_success(tmp_path):
    inner = FakeBackend(error=TransientBackendError("overloaded"))
    cached = CachedBackend(inner, root=tmp_path / "cache")
    req = make_request(tmp_path)
    with pytest.raises(TransientBackendError):
        cached.call(req)
    key = req.cache_key("claude_cli")
    assert not record_path(key, tmp_path / "cache").is_file()
    fp = failure_path(key, tmp_path / "cache")
    assert fp.is_file()
    row = json.loads(fp.read_text().splitlines()[0])
    assert row["error_class"] == "TransientBackendError" and row["cache_key"] == key
    assert cached.cached(req) is None

    # the next attempt still calls the backend, and a success then lands in the cache
    cached.inner = FakeBackend({"recovered": True})
    assert cached.call(req).structured == {"recovered": True}
    assert cached.cached(req) is not None


def test_a_half_written_record_is_not_served(tmp_path):
    req = make_request(tmp_path)
    key = req.cache_key("claude_cli")
    path = record_path(key, tmp_path / "cache")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text('{"cache_key": "' + key + '", "response": {}}')
    assert CachedBackend(FakeBackend(), root=tmp_path / "cache").cached(req) is None


def test_replay_is_strict(tmp_path):
    req = make_request(tmp_path)
    replay = ReplayBackend(roots=[tmp_path / "cache"])
    with pytest.raises(ReplayMiss, match="no recorded call"):
        replay.call(req)

    CachedBackend(FakeBackend({"from": "recording"}), root=tmp_path / "cache").call(req)
    assert ReplayBackend(roots=[tmp_path / "cache"]).call(req).structured == {"from": "recording"}

    # any change to the request is a miss, not a near-enough hit
    with pytest.raises(ReplayMiss):
        ReplayBackend(roots=[tmp_path / "cache"]).call(make_request(tmp_path, effort="high"))


def test_replay_reads_the_checked_in_probe_fixture():
    from legacy_reader.backends.replay import fixtures_dir

    fixture = fixtures_dir() / "probe_drilllog_p1.json"
    rec = json.loads(fixture.read_text())
    replay = ReplayBackend()
    assert replay.find(rec["cache_key"]) is not None
    served = replay.record(rec["cache_key"])
    assert served["response"]["structured"] == rec["response"]["structured"]


def test_replay_never_spawns_a_process(tmp_path, monkeypatch):
    import subprocess

    def boom(*a, **k):
        raise AssertionError("replay must never run a subprocess")

    monkeypatch.setattr(subprocess, "run", boom)
    with pytest.raises(ReplayMiss):
        ReplayBackend(roots=[tmp_path]).call(make_request(tmp_path))


def test_replay_pack_bundles_a_runs_envelopes(tmp_path, monkeypatch):
    """`lr replay pack --run <id>` turns a real run into fixtures the test suite can replay forever."""
    from legacy_reader.backends import replay as replay_mod

    cache_root = tmp_path / "cache"
    req = make_request(tmp_path)
    resp = CachedBackend(FakeBackend({"packed": True}), root=cache_root).call(req)

    run = tmp_path / "runs" / "R1"
    run.mkdir(parents=True)
    (run / "calls.jsonl").write_text(
        json.dumps({"status": "ok", "cache_key": resp.cache_key, "file_num": "74H09-0039", "page_no": 4})
        + "\n"
        + json.dumps({"status": "failed", "cache_key": "deadbeef", "file_num": "x", "page_no": 1}) + "\n")
    monkeypatch.setattr("legacy_reader.extract.run_dir", lambda run_id: tmp_path / "runs" / run_id)

    dest = tmp_path / "fixtures"
    written = replay_mod.pack_run("R1", dest=dest, cache_root=cache_root)
    assert len(written) == 1, "only successful calls are packed"
    assert written[0].name.startswith("R1_74H09-0039_p0004_")
    assert json.loads(written[0].read_text())["cache_key"] == resp.cache_key
    # and the packed fixture replays without the original cache
    assert replay_mod.ReplayBackend(roots=[dest]).call(req).structured == {"packed": True}
