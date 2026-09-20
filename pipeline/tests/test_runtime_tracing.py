"""Spans: nested under a run's trace in spans.jsonl, silent outside one, mirrored to MLflow when it is there."""

from __future__ import annotations

import threading
from pathlib import Path

import pytest

from legacy_reader.backends.base import ExtractionRequest, ExtractionResponse
from legacy_reader.backends.cache import CachedBackend
from legacy_reader.prospect import tools as T
from legacy_reader.runtime import tracing as TRC
from legacy_reader.runtime.tracing import current_trace_id, read_spans, set_attrs, span, trace


def by_name(run_dir: Path) -> dict[str, dict]:
    return {s["name"]: s for s in read_spans(run_dir)}


def test_spans_nest_under_the_run_with_parent_ids(tmp_path: Path) -> None:
    with trace("R1", "memo", run_dir=tmp_path / "R1", cell_id="0123_0045") as tr:
        assert current_trace_id() == tr.trace_id
        with span("cell:0123_0045", kind="cell", cell_id="0123_0045"):
            with span("tool:cell_features", kind="tool"):
                set_attrs(n_values=3)
            with span("model:prospect_memo_skeptic", kind="model", model="claude-sonnet-5"):
                pass
    assert current_trace_id() is None
    spans = read_spans(tmp_path / "R1")
    assert [s["name"] for s in spans] == ["tool:cell_features", "model:prospect_memo_skeptic", "cell:0123_0045", "R1"], \
        "each span is written as it closes, the root last"
    s = by_name(tmp_path / "R1")
    root, cell, tool, model = s["R1"], s["cell:0123_0045"], s["tool:cell_features"], s["model:prospect_memo_skeptic"]
    assert root["parent_id"] is None and root["kind"] == "memo" and root["attrs"]["cell_id"] == "0123_0045"
    assert cell["parent_id"] == root["span_id"] and tool["parent_id"] == cell["span_id"] == model["parent_id"]
    assert {sp["trace_id"] for sp in spans} == {tr.trace_id}
    assert tool["attrs"] == {"n_values": 3} and tool["kind"] == "tool"
    assert all(sp["status"] == "ok" and sp["error"] is None and sp["duration_ms"] >= 0 for sp in spans)
    assert set(root) == {"trace_id", "span_id", "parent_id", "name", "kind", "started_at", "ended_at", "duration_ms",
                         "attrs", "status", "error"}


def test_a_failure_inside_a_span_is_recorded_and_still_raised(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="boom"):
        with trace("R2", "chat", run_dir=tmp_path / "R2"):
            with span("tool:retrieve", kind="tool"):
                raise ValueError("boom")
    s = by_name(tmp_path / "R2")
    assert s["tool:retrieve"]["status"] == "error" and s["tool:retrieve"]["error"] == "ValueError: boom"
    assert s["R2"]["status"] == "error", "an error that leaves the run marks the run"


def test_span_outside_a_trace_is_a_no_op_that_still_runs_the_body(tmp_path: Path) -> None:
    ran = []
    with span("tool:coverage", kind="tool") as sp:
        set_attrs(anything=1)
        ran.append(sp)
    assert ran == [None] and current_trace_id() is None
    assert not list(tmp_path.rglob("spans.jsonl"))


def test_a_worker_thread_attaches_to_the_run_rather_than_starting_its_own(tmp_path: Path) -> None:
    def work() -> None:
        with span("cell:worker", kind="cell"):
            pass

    with trace("R3", "bench", run_dir=tmp_path / "R3"):
        t = threading.Thread(target=work)
        t.start()
        t.join()
    s = by_name(tmp_path / "R3")
    assert s["cell:worker"]["parent_id"] == s["R3"]["span_id"]
    assert s["cell:worker"]["trace_id"] == s["R3"]["trace_id"]


# ---------------------------------------------------------------- the instrumented call sites


class FakeBackend:
    family = "claude_cli"

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        return ExtractionResponse(structured={"ok": True}, envelope={}, backend="fake", backend_version="0",
                                  model_requested=req.model, model_resolved="claude-sonnet-5-20260101",
                                  num_turns=2, duration_s=1.5, usage={}, cost_usd=0.07,
                                  cache_key=req.cache_key(self.family))


def test_a_model_call_is_a_model_span_with_the_cache_key_and_the_cost(tmp_path: Path) -> None:
    img = tmp_path / "p.png"
    img.write_bytes(b"\x89PNG" + b"y" * 32)
    req = ExtractionRequest(task="prospect_chat", images=(img,), system_prompt="s", user_prompt="u",
                            schema={"type": "object"}, schema_version="1", prompt_version="1",
                            model="claude-sonnet-5", effort="low")
    backend = CachedBackend(FakeBackend(), root=tmp_path / "cache")
    with trace("R4", "chat", run_dir=tmp_path / "R4"):
        backend.call(req)
        backend.call(req)
    spans = [s for s in read_spans(tmp_path / "R4") if s["kind"] == "model"]
    assert len(spans) == 2 and {s["name"] for s in spans} == {"model:prospect_chat"}
    live, hit = spans
    assert live["attrs"]["cache_key"] == req.cache_key("claude_cli") == hit["attrs"]["cache_key"]
    assert live["attrs"]["from_cache"] is False and live["attrs"]["cost_usd"] == 0.07 and live["attrs"]["duration_s"] == 1.5
    assert live["attrs"]["model"] == "claude-sonnet-5" and live["attrs"]["model_resolved"] == "claude-sonnet-5-20260101"
    assert hit["attrs"]["from_cache"] is True
    backend.call(req)   # outside the trace: still served, nothing more written
    assert len(read_spans(tmp_path / "R4")) == 3


def test_a_tool_call_is_a_tool_span_with_its_name_argument_keys_and_value_count(tmp_path: Path, monkeypatch) -> None:
    def fake_tool(cell_id: str, radius_km: float = 25.0) -> T.ToolResult:
        return T.ToolResult("fake_tool", {"cell_id": cell_id}, rows=[{"a": 1}, {"a": 2}],
                            values={"c:x": {"id": "c:x"}, "c:y": {"id": "c:y"}, "c:z": {"id": "c:z"}})

    monkeypatch.setitem(T.REGISTRY, "fake_tool", fake_tool)
    with trace("R5", "memo", run_dir=tmp_path / "R5"):
        out = T.call("fake_tool", {"radius_km": 5.0, "cell_id": "0123_0045"})
        with pytest.raises(T.ToolError):
            T.call("fake_tool", {"nope": 1})
    assert len(out.values) == 3
    spans = [s for s in read_spans(tmp_path / "R5") if s["kind"] == "tool"]
    assert len(spans) == 2
    good, bad = spans
    assert good["name"] == "tool:fake_tool" and good["attrs"] == {"tool": "fake_tool", "arg_keys": ["cell_id", "radius_km"],
                                                                   "n_values": 3, "n_rows": 2}
    assert "0123_0045" not in str(good["attrs"]), "argument values never enter a span"
    assert bad["status"] == "error" and bad["error"].startswith("ToolError")
    assert T.call("fake_tool", {"cell_id": "x"}).tool == "fake_tool", "outside a trace the tool still runs"


# ---------------------------------------------------------------- the MLflow mirror


def test_spans_are_mirrored_to_mlflow_tracing_when_it_is_there(tmp_path: Path, monkeypatch) -> None:
    mlflow = pytest.importorskip("mlflow")
    from legacy_reader.prospect import tracking as TR

    monkeypatch.setenv("LR_TRACING_MLFLOW", "1")
    monkeypatch.setenv("LR_MLFLOW_URI", f"sqlite:///{tmp_path / 'mlflow.db'}")
    monkeypatch.setattr(TR, "artifact_dir", lambda: tmp_path / "mlruns")
    monkeypatch.setattr(TRC, "_mlflow_warned", False)
    with trace("R6", "memo", run_dir=tmp_path / "R6", cell_id="0123_0045") as tr:
        assert tr.mlflow is not None, "the mirror is on"
        with span("tool:cell_features", kind="tool", tool="cell_features"):
            set_attrs(n_values=2)
        with span("model:prospect_memo_skeptic", kind="model"):
            pass
    root = by_name(tmp_path / "R6")["R6"]
    assert root["attrs"].get("mlflow_trace_id"), "the run's line names the MLflow trace it was mirrored to"

    mlflow.flush_trace_async_logging()
    experiment = mlflow.get_experiment_by_name(TR.EXPERIMENT)
    found = mlflow.search_traces(locations=[experiment.experiment_id], return_type="list")
    ours = [t for t in found if t.info.trace_id == root["attrs"]["mlflow_trace_id"]]
    assert len(ours) == 1
    names = {s.name: s for s in ours[0].data.spans}
    assert {"R6", "tool:cell_features", "model:prospect_memo_skeptic"} <= set(names)
    assert names["tool:cell_features"].span_type == "TOOL" and names["model:prospect_memo_skeptic"].span_type == "LLM"
    assert names["tool:cell_features"].parent_id == names["R6"].span_id
    assert names["tool:cell_features"].attributes.get("n_values") == 2


def test_the_mirror_is_off_when_asked_and_a_broken_mirror_never_raises(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("LR_TRACING_MLFLOW", "0")
    with trace("R7", "chat", run_dir=tmp_path / "R7") as tr:
        assert tr.mlflow is None
    assert by_name(tmp_path / "R7")["R7"]["status"] == "ok"

    monkeypatch.setenv("LR_TRACING_MLFLOW", "1")
    monkeypatch.setattr(TRC, "_mlflow_warned", False)

    def broken():
        raise RuntimeError("no tracker today")

    monkeypatch.setattr("legacy_reader.prospect.tracking._mlflow", broken)
    logged: list[str] = []
    with trace("R8", "chat", run_dir=tmp_path / "R8", log=logged.append) as tr:
        assert tr.mlflow is None
        with span("tool:x", kind="tool"):
            pass
    with trace("R9", "chat", run_dir=tmp_path / "R9", log=logged.append):
        pass
    assert len(logged) == 1 and "no tracker today" in logged[0], "logged once, not once per run"
    assert len(read_spans(tmp_path / "R8")) == 2 and len(read_spans(tmp_path / "R9")) == 1
