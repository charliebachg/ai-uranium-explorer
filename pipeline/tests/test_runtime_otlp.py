"""The OTLP export: with an exporter in place of OTLP, a traced run's spans reach it with the file's ids, parents
and attributes; with no endpoint the SDK is never imported; a broken exporter warns once and never raises."""

from __future__ import annotations

import os
import subprocess
import sys
import threading
from pathlib import Path

import pytest

from legacy_reader import __version__
from legacy_reader.runtime import otlp as OTLP
from legacy_reader.runtime import tracing as TRC
from legacy_reader.runtime.tracing import read_spans, set_attrs, span, trace


@pytest.fixture
def memory(monkeypatch: pytest.MonkeyPatch):
    """The SDK's in-memory exporter installed in place of OTLP for the test, and dropped after it."""
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    monkeypatch.setenv("LR_TRACING_MLFLOW", "0")
    monkeypatch.delenv("LR_OTLP_ENDPOINT", raising=False)
    exporter = InMemorySpanExporter()
    OTLP.install(exporter, log=lambda *a: None)
    yield exporter
    OTLP.reset()


def exported(exporter) -> dict[str, object]:
    return {s.name: s for s in exporter.get_finished_spans()}


def test_a_traced_run_reaches_the_exporter_with_the_files_ids_parents_and_attributes(tmp_path: Path, memory) -> None:
    from opentelemetry.trace import SpanKind, StatusCode

    with trace("R1", "memo", run_dir=tmp_path / "R1", cell_id="0123_0045", session_id="s-1") as tr:
        assert tr.otlp is not None, "the bridge is on"
        with span("cell:0123_0045", kind="cell", cell_id="0123_0045"):
            with span("tool:cell_features", kind="tool", tool="cell_features", arg_keys=["cell_id", "radius_km"]):
                set_attrs(n_values=3, n_rows=2)
            with span("model:prospect_memo_skeptic", kind="model", model="claude-sonnet-5", cache_key="k" * 64):
                set_attrs(from_cache=False, cost_usd=0.07, duration_s=1.5, problems=[])
            with pytest.raises(ValueError, match="boom"):
                with span("gate:check", kind="gate", nothing=None):
                    raise ValueError("boom")
    got = exported(memory)
    assert set(got) == {"R1", "cell:0123_0045", "tool:cell_features", "model:prospect_memo_skeptic", "gate:check"}
    file = {s["name"]: s for s in read_spans(tmp_path / "R1")}
    for name, s in got.items():
        assert f"{s.context.trace_id:032x}" == tr.trace_id == file[name]["trace_id"], "the run's trace id is the backend's"
        assert f"{s.context.span_id:016x}" == file[name]["span_id"], f"{name}: the file's span id is the backend's"
        parent = f"{s.parent.span_id:016x}" if s.parent is not None else None
        assert parent == file[name]["parent_id"], f"{name}: same parent as the file"
        assert s.resource.attributes["service.name"] == "legacy-reader"
        assert s.resource.attributes["service.version"] == __version__
        assert s.attributes["run_id"] == "R1" and s.attributes["kind"] == file[name]["kind"]
        assert s.start_time <= s.end_time
        assert abs((s.end_time - s.start_time) / 1e6 - file[name]["duration_ms"]) < 1.0, "the file's duration, to the ms"
    root, tool, model, gate = got["R1"], got["tool:cell_features"], got["model:prospect_memo_skeptic"], got["gate:check"]
    assert root.parent is None and root.attributes["run_kind"] == "memo" and root.attributes["cell_id"] == "0123_0045"
    assert root.attributes["session_id"] == "s-1"
    assert tool.attributes["tool"] == "cell_features" and tool.attributes["n_values"] == 3 and tool.attributes["n_rows"] == 2
    assert tuple(tool.attributes["arg_keys"]) == ("cell_id", "radius_km"), "a list of strings stays a sequence"
    assert model.attributes["cache_key"] == "k" * 64 and model.attributes["cost_usd"] == 0.07
    assert model.attributes["duration_s"] == 1.5 and model.attributes["from_cache"] is False
    assert model.attributes["problems"] == "[]", "an empty list has no element type; it goes as JSON"
    assert model.kind == SpanKind.CLIENT and tool.kind == SpanKind.INTERNAL, "a model call leaves the machine"
    assert gate.status.status_code == StatusCode.ERROR and gate.attributes["error"] == "ValueError: boom"
    assert "nothing" not in gate.attributes, "a None attribute is left out rather than sent as a string"
    assert root.status.status_code == StatusCode.OK and tool.status.status_code == StatusCode.OK
    assert file["gate:check"]["status"] == "error", "and spans.jsonl says the same"


def test_a_worker_thread_span_is_exported_under_the_run(tmp_path: Path, memory) -> None:
    def work() -> None:
        with span("cell:worker", kind="cell"):
            pass

    with trace("R2", "bench", run_dir=tmp_path / "R2"):
        t = threading.Thread(target=work)
        t.start()
        t.join()
    got = exported(memory)
    assert f"{got['cell:worker'].parent.span_id:016x}" == f"{got['R2'].context.span_id:016x}"
    assert got["cell:worker"].context.trace_id == got["R2"].context.trace_id


def test_with_no_endpoint_the_sdk_is_never_touched(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("LR_OTLP_ENDPOINT", raising=False)
    monkeypatch.setenv("LR_TRACING_MLFLOW", "0")
    OTLP.reset()

    def never(*a, **k):
        raise AssertionError("the bridge was built with no endpoint set")

    monkeypatch.setattr(OTLP, "_build", never)
    with trace("R3", "chat", run_dir=tmp_path / "R3") as tr:
        assert tr.otlp is None
        with span("tool:x", kind="tool") as sp:
            assert sp is not None and sp.otel is None
    assert len(read_spans(tmp_path / "R3")) == 2

    # in a fresh process: a traced run imports no opentelemetry module at all (the mirror off, so MLflow's own
    # use of the SDK does not confound it)
    code = ("import sys, pathlib\n"
            "from legacy_reader.runtime.tracing import trace, span\n"
            f"with trace('R', 'chat', run_dir=pathlib.Path({str(tmp_path / 'sub')!r})):\n"
            "    with span('tool:x'): pass\n"
            "print(sorted(m for m in sys.modules if m.startswith('opentelemetry')))\n")
    env = {k: v for k, v in os.environ.items() if k not in ("LR_OTLP_ENDPOINT", "LR_OTLP_HEADERS")}
    env["LR_TRACING_MLFLOW"] = "0"
    out = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "[]", out.stdout


def test_the_endpoint_and_headers_come_from_the_environment_and_the_header_values_stay_out_of_logs(monkeypatch) -> None:
    monkeypatch.delenv("LR_OTLP_ENDPOINT", raising=False)
    assert OTLP.endpoint() is None
    monkeypatch.setenv("LR_OTLP_ENDPOINT", "http://tempo:4318")
    assert OTLP.endpoint() == "http://tempo:4318/v1/traces", "a bare host gets the OTLP/HTTP traces path"
    monkeypatch.setenv("LR_OTLP_ENDPOINT", "https://otlp.example.com/v1/traces")
    assert OTLP.endpoint() == "https://otlp.example.com/v1/traces", "a full URL is used as given"
    monkeypatch.setenv("LR_OTLP_HEADERS", "Authorization=Basic%20abc==, x-scope-orgid=lr ,broken")
    assert OTLP.headers() == {"Authorization": "Basic abc==", "x-scope-orgid": "lr"}
    monkeypatch.delenv("LR_OTLP_HEADERS")
    assert OTLP.headers() == {}


def test_a_failing_exporter_warns_once_and_never_raises(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    from opentelemetry.sdk.trace.export import SpanExporter

    monkeypatch.setenv("LR_TRACING_MLFLOW", "0")
    monkeypatch.delenv("LR_OTLP_ENDPOINT", raising=False)

    class Broken(SpanExporter):
        calls = 0

        def export(self, spans):
            Broken.calls += 1
            raise ConnectionError("backend down")

        def shutdown(self):
            pass

        def force_flush(self, timeout_millis=30000):
            return True

    logged: list[str] = []
    OTLP.install(Broken(), log=logged.append)
    try:
        with trace("R4", "chat", run_dir=tmp_path / "R4", log=logged.append):
            with span("tool:x", kind="tool"):
                pass
        with trace("R5", "chat", run_dir=tmp_path / "R5", log=logged.append):
            pass
    finally:
        OTLP.reset()
    assert Broken.calls >= 1, "the run-end flush handed the batch to the exporter"
    warnings = [line for line in logged if "OTLP export off" in line]
    assert len(warnings) == 1 and "backend down" in warnings[0], logged
    assert len(read_spans(tmp_path / "R4")) == 2 and len(read_spans(tmp_path / "R5")) == 1, "the file is untouched"


def test_a_bridge_that_breaks_mid_run_is_dropped_for_the_run_and_the_file_still_gets_every_span(tmp_path, memory, monkeypatch) -> None:
    logged: list[str] = []
    monkeypatch.setattr(OTLP, "_warned", False)

    def broken_end(span, run_id):
        raise RuntimeError("no more")

    with trace("R6", "chat", run_dir=tmp_path / "R6", log=logged.append) as tr:
        monkeypatch.setattr(tr.otlp, "end", broken_end)
        with span("tool:x", kind="tool"):
            pass
        assert tr.otlp is None, "off for the rest of the run"
        with span("tool:y", kind="tool"):
            pass
    assert [s["name"] for s in read_spans(tmp_path / "R6")] == ["tool:x", "tool:y", "R6"]
    assert TRC.current_trace_id() is None
