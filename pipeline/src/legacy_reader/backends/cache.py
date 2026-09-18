"""On-disk call cache. A cached success is replayable; a failure is recorded but never served.

Layout (under `data/cache/`):
    calls/<k[:2]>/<k>.json       one successful call, request + response + raw envelope
    failures/<k[:2]>/<k>.jsonl   one line per failed attempt (appended, never read back as a success)

The key is `ExtractionRequest.cache_key(inner.family)`: task, backend family, image sha256s, render
params, prompt version, schema version, model, effort and carry-context hash. The staged temp path,
the wall clock and the run id are deliberately outside it, so a re-run is free and resumable.
"""

from __future__ import annotations

import datetime as dt
import json
import inspect
import os
import tempfile
from pathlib import Path
from typing import Any, Callable

from ..ids import sha256_file
from ..paths import PATHS
from .base import Backend, BackendError, ExtractionRequest, ExtractionResponse

RECORD_VERSION = "call-record/v1"


def streams(backend: object) -> bool:
    """Whether a backend's call() will take a delta callback. Asked rather than assumed, so a backend that
    cannot stream is called the way it expects instead of being handed an argument it will reject."""
    try:
        return "on_delta" in inspect.signature(backend.call).parameters  # type: ignore[attr-defined]
    except (TypeError, ValueError, AttributeError):
        return False


def calls_dir(root: Path | None = None) -> Path:
    return (root or PATHS.cache) / "calls"


def failures_dir(root: Path | None = None) -> Path:
    return (root or PATHS.cache) / "failures"


def record_path(key: str, root: Path | None = None) -> Path:
    return calls_dir(root) / key[:2] / f"{key}.json"


def failure_path(key: str, root: Path | None = None) -> Path:
    return failures_dir(root) / key[:2] / f"{key}.jsonl"


def write_atomic(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, suffix=".part")
    try:
        with os.fdopen(fd, "w") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def request_summary(req: ExtractionRequest) -> dict[str, Any]:
    return {
        "task": req.task,
        "model": req.model,
        "effort": req.effort,
        "prompt_version": req.prompt_version,
        "schema_version": req.schema_version,
        "context_hash": req.context_hash,
        "render_params": req.render_params,
        "images": [{"name": p.name, "sha256": sha256_file(p)} for p in req.images],
    }


def build_record(req: ExtractionRequest, resp: ExtractionResponse, extra: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "version": RECORD_VERSION,
        "cache_key": resp.cache_key,
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "request": request_summary(req),
        "response": {
            "structured": resp.structured,
            "backend": resp.backend,
            "backend_version": resp.backend_version,
            "model_requested": resp.model_requested,
            "model_resolved": resp.model_resolved,
            "num_turns": resp.num_turns,
            "duration_s": resp.duration_s,
            "usage": resp.usage,
            "cost_usd": resp.cost_usd,
        },
        "envelope": resp.envelope,
        **(extra or {}),
    }


def response_from_record(rec: dict[str, Any], key: str) -> ExtractionResponse:
    r = rec["response"]
    return ExtractionResponse(
        structured=r["structured"],
        envelope=rec.get("envelope") or {},
        backend=r.get("backend", "unknown"),
        backend_version=r.get("backend_version", "unknown"),
        model_requested=r.get("model_requested", ""),
        model_resolved=r.get("model_resolved"),
        num_turns=r.get("num_turns"),
        duration_s=r.get("duration_s", 0.0),
        usage=r.get("usage") or {},
        cost_usd=r.get("cost_usd"),
        cache_key=key,
        from_cache=True,
    )


class CachedBackend:
    """Wraps any backend with the on-disk cache. `family` mirrors the inner backend, so the key is stable."""

    def __init__(self, inner: Backend, root: Path | None = None, refresh: bool = False):
        self.inner = inner
        self.root = root or PATHS.cache
        self.refresh = refresh
        self.hits = 0
        self.misses = 0

    @property
    def family(self) -> str:
        return self.inner.family

    def key_for(self, req: ExtractionRequest) -> str:
        return req.cache_key(self.inner.family)

    def cached(self, req: ExtractionRequest) -> ExtractionResponse | None:
        key = self.key_for(req)
        path = record_path(key, self.root)
        if not path.is_file():
            return None
        try:
            rec = json.loads(path.read_text())
        except json.JSONDecodeError:
            return None
        if not isinstance(rec.get("response", {}).get("structured"), dict):
            return None  # a half-written or failure-shaped record is not a success
        return response_from_record(rec, key)

    def invalidate(self, req: ExtractionRequest, error: BaseException, attempt: int = 1) -> bool:
        """Drop a cached answer the caller could not use (schema-invalid) and record it as a failure.

        A record that does not validate is not a usable success: leaving it in place would make the
        schema-invalid retry replay the same bad answer, and every later run inherit it.
        """
        key = self.key_for(req)
        path = record_path(key, self.root)
        existed = path.is_file()
        if existed:
            path.unlink()
        self.record_failure(req, error, attempt)
        return existed

    def record_failure(self, req: ExtractionRequest, error: BaseException, attempt: int) -> None:
        key = self.key_for(req)
        line = json.dumps({
            "cache_key": key,
            "failed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "attempt": attempt,
            "error_class": type(error).__name__,
            "error": str(error)[:2000],
            "request": request_summary(req),
        }, separators=(",", ":"))
        path = failure_path(key, self.root)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as f:
            f.write(line + "\n")

    def call(
        self,
        req: ExtractionRequest,
        attempt: int = 1,
        on_delta: Callable[[str], None] | None = None,
    ) -> ExtractionResponse:
        """`on_delta` is forwarded to a backend that can stream. A cache hit never streams, because nothing is
        being generated — it returns the recorded answer whole, which is the honest thing for it to do."""
        if not self.refresh:
            hit = self.cached(req)
            if hit is not None:
                self.hits += 1
                return hit
        self.misses += 1
        try:
            resp = self.inner.call(req, on_delta=on_delta) if on_delta and streams(self.inner) \
                else self.inner.call(req)
        except BackendError as e:
            self.record_failure(req, e, attempt)
            raise
        except Exception as e:  # unexpected: still recorded, still never cached
            self.record_failure(req, e, attempt)
            raise
        write_atomic(record_path(resp.cache_key, self.root),
                     json.dumps(build_record(req, resp), separators=(",", ":")) + "\n")
        return resp
