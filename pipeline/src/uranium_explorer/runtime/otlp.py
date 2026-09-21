"""OTLP export of a run's spans: the same spans `spans.jsonl` holds, sent to whatever backend is configured.

The instrumentation is the in-house `Trace`/`Span` in `tracing.py`; this module is the bridge from it to the
OpenTelemetry SDK, so the backend is a configuration and not a code change. With `UE_OTLP_ENDPOINT`
set, every span is started and ended alongside an SDK span carrying the same ids (the file's trace and span
ids are already 128 and 64 random bits, so a backend's span is the file's span, not a lookalike), the same
attributes, the resource `service.name=ai-uranium-explorer` and the pipeline version, and is exported over OTLP/HTTP
(protobuf) in batches from a bounded queue. With it unset, `bridge()` returns None and nothing here imports the
SDK or the exporter.

Best-effort by design, like the MLflow mirror: the queue drops rather than blocks when it is full, a failure is
logged once and never raises, and the flush at the end of a run is bounded. `spans.jsonl` stays the ground truth
either way. `UE_OTLP_HEADERS` carries an auth header for a hosted backend; its value is never logged.
"""

from __future__ import annotations

import atexit
import logging
import os
import threading
from typing import TYPE_CHECKING, Any, Callable
from urllib.parse import unquote, urlsplit

if TYPE_CHECKING:  # the SDK is imported only when a bridge is built
    from .tracing import Span

SERVICE_NAME = "ai-uranium-explorer"
#: the OTLP/HTTP traces path, appended when the endpoint names only a host (`http://tempo:4318`)
TRACES_PATH = "/v1/traces"
#: our span kinds mapped to the SDK's: a model call leaves the machine, everything else is local work
CLIENT_KINDS = {"model"}
#: the bounded queue: spans beyond it are dropped, never waited for
MAX_QUEUE = 2048
BATCH_SIZE = 256
SCHEDULE_MS = 2000
#: how long the run-end flush and each export attempt may take before giving up
FLUSH_MS = 5000
EXPORT_TIMEOUT_S = 5.0

_lock = threading.Lock()
_bridge: Bridge | None = None
_warned = False
_log: Callable[[str], None] = print     # where the SDK's own failure lines go: the current bridge's log
_filtered = False                       # the SDK loggers carry our filter once per process
_exit_registered = False                # one exit hook for whichever bridge is current, not one per provider


def endpoint() -> str | None:
    """The traces URL from `UE_OTLP_ENDPOINT`, with `/v1/traces` added when only a host is given; None when unset."""
    raw = os.environ.get("UE_OTLP_ENDPOINT", "").strip()
    if not raw:
        return None
    parts = urlsplit(raw)
    if parts.path in ("", "/"):
        return raw.rstrip("/") + TRACES_PATH
    return raw


def headers() -> dict[str, str]:
    """`UE_OTLP_HEADERS` as `name=value,name2=value2` (percent-encoded values allowed, as the OTel env form is)."""
    out: dict[str, str] = {}
    for item in os.environ.get("UE_OTLP_HEADERS", "").split(","):
        if "=" not in item:
            continue
        name, value = item.split("=", 1)
        if name.strip():
            out[name.strip()] = unquote(value.strip())
    return out


def warn_once(log: Callable[[str], None], what: str, err: BaseException | str) -> None:
    global _warned
    if not _warned:
        _warned = True
        detail = f"{type(err).__name__}: {str(err)[:200]}" if isinstance(err, BaseException) else str(err)[:200]
        log(f"  tracing: OTLP export off after {what}: {detail}")


class _Once(logging.Filter):
    """The SDK's exporter thread logs every failed batch; this turns the first into one line of ours and swallows
    the rest, so a backend that is down costs one warning and not one per batch."""

    def filter(self, record: logging.LogRecord) -> bool:
        if record.levelno >= logging.WARNING:
            try:
                what = record.getMessage()
                if record.exc_info and record.exc_info[1] is not None:
                    err = record.exc_info[1]
                    what = f"{what} {type(err).__name__}: {err}"
                warn_once(_log, "an export", what)
            except Exception:  # noqa: BLE001
                pass
        return False


def _filter_sdk_logs() -> None:
    global _filtered
    if _filtered:
        return
    _filtered = True
    once = _Once()
    for name in ("opentelemetry.exporter.otlp.proto.http.trace_exporter", "opentelemetry.sdk._shared_internal"):
        logging.getLogger(name).addFilter(once)


def _attr(value: Any) -> Any:
    """An attribute value the SDK accepts: a primitive, a homogeneous tuple of primitives, else its JSON."""
    import json

    if isinstance(value, (bool, int, float, str)):
        return value
    if isinstance(value, (list, tuple)):
        items = tuple(value)
        if items and all(isinstance(v, str) for v in items):
            return items
        if items and all(isinstance(v, bool) for v in items):
            return items
        if items and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in items):
            return items
        return json.dumps(items, default=str)
    return json.dumps(value, default=str)


def attributes(span: Span) -> dict[str, Any]:
    """What the backend sees of a span: its attributes as `spans.jsonl` has them, plus the file's `kind` and the
    run id on every span (OTLP has no trace-level attributes; the run id is how a backend selects a run)."""
    out: dict[str, Any] = {"kind": span.kind}
    for key, value in span.attrs.items():
        if value is None:
            continue
        out[key] = _attr(value)
    return out


class Bridge:
    """One SDK tracer provider for the process; `start`/`end` follow the in-house span, `flush` closes a run."""

    def __init__(self, provider: Any, ids: Any, where: str) -> None:
        from .. import __version__

        self.provider = provider
        self.tracer = provider.get_tracer("uranium_explorer", __version__)
        self.ids = ids
        self.where = where

    def start(self, span: Span, parent: Span | None, run_id: str) -> Any:
        from opentelemetry import trace as api
        from opentelemetry.context import Context

        # an explicit context: the parent when there is one, else empty, so a span never nests under whatever the
        # thread's OpenTelemetry context happens to hold (MLflow's mirror uses the same API)
        context = api.set_span_in_context(parent.otel) if parent is not None and parent.otel is not None else Context()
        kind = api.SpanKind.CLIENT if span.kind in CLIENT_KINDS else api.SpanKind.INTERNAL
        self.ids.next(span.trace_id, span.span_id)
        attrs = attributes(span)
        attrs.setdefault("run_id", run_id)
        return self.tracer.start_span(span.name, context=context, kind=kind, attributes=attrs,
                                      start_time=span.t_epoch_ns, record_exception=False, set_status_on_exception=False)

    def end(self, span: Span, run_id: str) -> None:
        from opentelemetry.trace import Status, StatusCode

        otel = span.otel
        attrs = attributes(span)
        attrs.setdefault("run_id", run_id)
        if span.error:
            attrs["error"] = span.error
        otel.set_attributes(attrs)
        otel.set_status(Status(StatusCode.ERROR, span.error) if span.status == "error" else Status(StatusCode.OK))
        end_ns = span.t_epoch_ns + int((span.duration_ms or 0.0) * 1_000_000)
        otel.end(end_time=max(end_ns, span.t_epoch_ns))

    def flush(self, timeout_ms: int = FLUSH_MS) -> bool:
        """Push what is queued, waiting at most `timeout_ms`; False when the backend did not take it in time."""
        return bool(self.provider.force_flush(timeout_millis=timeout_ms))

    def shutdown(self) -> None:
        self.provider.shutdown()


def _ids_class() -> type:
    from opentelemetry.sdk.trace.id_generator import IdGenerator, RandomIdGenerator

    class Ids(IdGenerator):
        """Hands the SDK the ids the in-house span already has, set on this thread just before `start_span`;
        anything else the SDK asks for is random, as it would be without us."""

        def __init__(self) -> None:
            self.local = threading.local()
            self.random = RandomIdGenerator()

        def next(self, trace_id: str, span_id: str) -> None:
            self.local.trace_id = int(trace_id, 16) or None
            self.local.span_id = int(span_id, 16) or None

        def generate_trace_id(self) -> int:
            return getattr(self.local, "trace_id", None) or self.random.generate_trace_id()

        def generate_span_id(self) -> int:
            return getattr(self.local, "span_id", None) or self.random.generate_span_id()

        def is_trace_id_random(self) -> bool:
            return True   # uuid4: the low seven bytes are random, which is what the flag promises

    return Ids


def _build(exporter: Any, where: str, log: Callable[[str], None]) -> Bridge:
    global _log
    from opentelemetry.sdk.resources import Resource
    from opentelemetry.sdk.trace import TracerProvider
    from opentelemetry.sdk.trace.export import BatchSpanProcessor

    from .. import __version__

    _log = log
    _filter_sdk_logs()
    _register_exit()
    ids = _ids_class()()
    resource = Resource.create({"service.name": SERVICE_NAME, "service.version": __version__})
    # shutdown_on_exit off: the SDK would register one hook per provider, and a test builds several
    provider = TracerProvider(resource=resource, id_generator=ids, shutdown_on_exit=False)
    provider.add_span_processor(BatchSpanProcessor(
        exporter, max_queue_size=MAX_QUEUE, schedule_delay_millis=SCHEDULE_MS, max_export_batch_size=BATCH_SIZE,
        export_timeout_millis=FLUSH_MS))
    return Bridge(provider, ids, where)


def _at_exit() -> None:
    """Whatever is still queued goes out before the process ends, within the exporter's own timeout."""
    with _lock:
        current = _bridge
    if current is not None:
        try:
            current.shutdown()
        except Exception:  # noqa: BLE001
            pass


def _register_exit() -> None:
    global _exit_registered
    if not _exit_registered:
        _exit_registered = True
        atexit.register(_at_exit)


def install(exporter: Any, log: Callable[[str], None] = print) -> Bridge:
    """Use this SDK span exporter (a test's InMemorySpanExporter, say) in place of OTLP, for the process."""
    global _bridge
    with _lock:
        if _bridge is not None:
            _bridge.shutdown()
        _bridge = _build(exporter, f"{type(exporter).__name__}", log)
        return _bridge


def reset() -> None:
    """Drop the process's bridge (a test's teardown); the next trace builds one again from the environment."""
    global _bridge, _warned, _log
    with _lock:
        if _bridge is not None:
            try:
                _bridge.shutdown()
            except Exception:  # noqa: BLE001
                pass
        _bridge = None
        _warned = False
        _log = print


def bridge(log: Callable[[str], None] = print) -> Bridge | None:
    """The process's bridge, built on first use when `UE_OTLP_ENDPOINT` is set (or one was installed); else None.

    Building it is where the SDK and the exporter are imported; a failure there is logged once and leaves the
    run untraced over OTLP, with `spans.jsonl` unaffected."""
    global _bridge
    with _lock:
        if _bridge is not None:
            return _bridge
    url = endpoint()
    if url is None:
        return None
    try:
        from opentelemetry.exporter.otlp.proto.http.trace_exporter import OTLPSpanExporter

        exporter = OTLPSpanExporter(endpoint=url, headers=headers() or None, timeout=EXPORT_TIMEOUT_S)
        with _lock:
            if _bridge is None:
                _bridge = _build(exporter, url, log)
                parts = urlsplit(url)   # the host only: a hosted backend's path may carry a token
                log(f"  tracing: OTLP export on -> {parts.scheme}://{parts.netloc}")
            return _bridge
    except Exception as err:  # noqa: BLE001 - best-effort, like the MLflow mirror
        warn_once(log, "setup", err)
        return None


__all__ = ["Bridge", "attributes", "bridge", "endpoint", "headers", "install", "reset", "warn_once"]
