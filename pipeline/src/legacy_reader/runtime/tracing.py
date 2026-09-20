"""Spans for runs, cells, tool calls and model calls: what per-stage metrics are computed from.

A run is a trace; inside it, every cell, tool call, model call and gate check is a span with a parent, a
duration, its attributes and whether it failed. `<run_dir>/spans.jsonl` is the source of truth — one line per
span, appended as each one closes — because a file beside the run's call log is what the benchmark reads and
what survives a tracing backend that is switched off, absent or broken.

When MLflow is importable and `LR_TRACING_MLFLOW` is not "0", the same spans are mirrored to MLflow Tracing
(OpenTelemetry underneath, stored beside the experiment runs and the registry in the same SQLite file), so a
trace can be browsed and linked to the run that produced it. The mirror is best-effort by design: a failure
there is logged once and never raises, because a missing trace viewer must not fail a memo.

Spans in worker threads (the scheduler's pool) attach to the run's root span rather than starting a trace of
their own: context variables do not cross into a pool thread, so the active trace is also held process-wide.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import os
import threading
import time
import uuid
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from .runs import run_dir as default_run_dir

#: our span kinds, and the MLflow span type each is mirrored as
SPAN_TYPES: dict[str, str] = {"tool": "TOOL", "model": "LLM", "cell": "CHAIN", "gate": "UNKNOWN"}
ROOT_SPAN_TYPE = "CHAIN"


@dataclass
class Span:
    trace_id: str
    span_id: str
    parent_id: str | None
    name: str
    kind: str
    started_at: str
    attrs: dict[str, Any] = field(default_factory=dict)
    ended_at: str | None = None
    duration_ms: float | None = None
    status: str = "ok"
    error: str | None = None
    t0: float = field(default_factory=time.monotonic, repr=False)
    mirror: Any = field(default=None, repr=False)   # the MLflow LiveSpan, when mirrored

    def as_record(self) -> dict[str, Any]:
        return {
            "trace_id": self.trace_id, "span_id": self.span_id, "parent_id": self.parent_id,
            "name": self.name, "kind": self.kind, "started_at": self.started_at, "ended_at": self.ended_at,
            "duration_ms": self.duration_ms, "attrs": self.attrs, "status": self.status, "error": self.error,
        }


@dataclass
class Trace:
    trace_id: str
    run_id: str
    kind: str
    path: Path
    root: Span | None = None
    mlflow: Any = None                        # the configured mlflow module, or None when not mirroring
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)


_trace_var: contextvars.ContextVar[Trace | None] = contextvars.ContextVar("lr_trace", default=None)
_span_var: contextvars.ContextVar[Span | None] = contextvars.ContextVar("lr_span", default=None)
_active: Trace | None = None                  # process-wide fallback for pool threads
_mlflow_warned = False


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds")


def current_trace() -> Trace | None:
    return _trace_var.get() or _active


def current_trace_id() -> str | None:
    tr = current_trace()
    return tr.trace_id if tr else None


def current_span() -> Span | None:
    """The span a new one nests under: the context's, else the run's root (a pool thread has no context)."""
    sp = _span_var.get()
    if sp is not None:
        return sp
    tr = current_trace()
    return tr.root if tr else None


# ---------------------------------------------------------------- the MLflow mirror


def _warn_once(log: Callable[[str], None], what: str, err: BaseException) -> None:
    global _mlflow_warned
    if not _mlflow_warned:
        _mlflow_warned = True
        log(f"  tracing: MLflow mirror off after {what}: {type(err).__name__}: {str(err)[:200]}")


def _mlflow_module(log: Callable[[str], None]) -> Any:
    """The configured mlflow module, or None when the mirror is off, absent or broken."""
    if os.environ.get("LR_TRACING_MLFLOW", "1").strip() == "0":
        return None
    try:
        from ..prospect.tracking import _mlflow
        return _mlflow()
    except Exception as err:  # noqa: BLE001 - the mirror is best-effort by design
        _warn_once(log, "setup", err)
        return None


def _mirror_start(tr: Trace, span: Span, parent: Span | None, span_type: str) -> Any:
    if tr.mlflow is None:
        return None
    try:
        return tr.mlflow.start_span_no_context(
            span.name, span_type=span_type, parent_span=parent.mirror if parent else None,
            attributes=dict(span.attrs),
        )
    except Exception as err:  # noqa: BLE001
        _warn_once(print, "start_span", err)
        tr.mlflow = None
        return None


def _mirror_end(tr: Trace, span: Span) -> None:
    if span.mirror is None:
        return
    try:
        attrs = dict(span.attrs)
        if span.error:
            attrs["error"] = span.error
        span.mirror.end(attributes=attrs, status="ERROR" if span.status == "error" else "OK")
    except Exception as err:  # noqa: BLE001
        _warn_once(print, "end_span", err)
        tr.mlflow = None


# ---------------------------------------------------------------- spans


def _finish(tr: Trace, span: Span) -> None:
    span.ended_at = _now()
    span.duration_ms = round((time.monotonic() - span.t0) * 1000.0, 3)
    _mirror_end(tr, span)
    line = json.dumps(span.as_record(), separators=(",", ":"), default=str)
    with tr.lock:
        tr.path.parent.mkdir(parents=True, exist_ok=True)
        with tr.path.open("a") as fh:
            fh.write(line + "\n")


def _fail(span: Span, err: BaseException) -> None:
    span.status = "error"
    span.error = f"{type(err).__name__}: {err}"[:2000]


@contextmanager
def trace(run_id: str, kind: str, run_dir: Path | None = None, log: Callable[[str], None] = print,
          **attrs: Any) -> Iterator[Trace]:
    """The root span of a run. Spans opened inside (in any thread) nest under it; its line is written last."""
    global _active
    directory = run_dir or default_run_dir(run_id)
    directory.mkdir(parents=True, exist_ok=True)
    tr = Trace(trace_id=uuid.uuid4().hex, run_id=run_id, kind=kind, path=directory / "spans.jsonl")
    tr.mlflow = _mlflow_module(log)
    root = Span(trace_id=tr.trace_id, span_id=uuid.uuid4().hex[:16], parent_id=None, name=run_id, kind=kind,
                started_at=_now(), attrs={"run_id": run_id, "run_kind": kind, **attrs})
    root.mirror = _mirror_start(tr, root, None, ROOT_SPAN_TYPE)
    if root.mirror is not None:
        try:
            root.attrs["mlflow_trace_id"] = root.mirror.trace_id
        except Exception:  # noqa: BLE001
            pass
    tr.root = root
    previous = _active
    _active = tr
    t_token = _trace_var.set(tr)
    s_token = _span_var.set(root)
    try:
        yield tr
    except BaseException as err:
        _fail(root, err)
        raise
    finally:
        _span_var.reset(s_token)
        _trace_var.reset(t_token)
        _active = previous
        _finish(tr, root)
        if tr.mlflow is not None:
            try:
                tr.mlflow.flush_trace_async_logging()
            except Exception as err:  # noqa: BLE001
                _warn_once(log, "flush", err)


@contextmanager
def span(name: str, kind: str = "tool", **attrs: Any) -> Iterator[Span | None]:
    """One step inside a run. With no active trace the body still runs and nothing is written."""
    tr = current_trace()
    if tr is None:
        yield None
        return
    parent = current_span()
    sp = Span(trace_id=tr.trace_id, span_id=uuid.uuid4().hex[:16], parent_id=parent.span_id if parent else None,
              name=name, kind=kind, started_at=_now(), attrs=dict(attrs))
    sp.mirror = _mirror_start(tr, sp, parent, SPAN_TYPES.get(kind, "UNKNOWN"))
    token = _span_var.set(sp)
    try:
        yield sp
    except BaseException as err:
        _fail(sp, err)
        raise
    finally:
        _span_var.reset(token)
        _finish(tr, sp)


def set_attrs(**attrs: Any) -> None:
    """Add attributes to the span this context is in. Outside any span, nothing happens."""
    sp = _span_var.get()
    if sp is None:
        return
    sp.attrs.update(attrs)
    if sp.mirror is not None:
        try:
            sp.mirror.set_attributes(dict(attrs))
        except Exception:  # noqa: BLE001 - recorded in spans.jsonl regardless
            pass


def read_spans(run_dir: Path) -> list[dict[str, Any]]:
    """The spans a run wrote, in the order they closed."""
    path = run_dir / "spans.jsonl"
    if not path.is_file():
        return []
    return [json.loads(ln) for ln in path.read_text().splitlines() if ln.strip()]
