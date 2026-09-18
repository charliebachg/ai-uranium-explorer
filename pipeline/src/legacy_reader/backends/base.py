"""Backend interface and error taxonomy shared by the claude CLI, replay and (later) API adapters."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..ids import sha256_file, sha256_json


@dataclass(frozen=True)
class ExtractionRequest:
    task: str                      # "extract_page", "triage", "probe"
    images: tuple[Path, ...]       # canonical page images (hashed for the cache key)
    system_prompt: str
    user_prompt: str
    schema: dict[str, Any]
    schema_version: str
    prompt_version: str
    model: str                     # full model id, never an alias
    effort: str                    # low | medium | high | xhigh | max
    context_hash: str = ""         # carry context from a previous page, if any
    render_params: dict[str, Any] = field(default_factory=dict)
    #: (source path, name it is staged under) for anything that is not a page image: an evidence bundle, a
    #: handbook, the results of a tool call. Hashed into the cache key like the images, so a changed bundle
    #: can never be answered from an older run's cache.
    stage_files: tuple[tuple[Path, str], ...] = ()

    def image_hashes(self) -> list[str]:
        return [sha256_file(p) for p in self.images]

    def stage_hashes(self) -> list[list[str]]:
        return [[name, sha256_file(path)] for path, name in self.stage_files]

    def cache_key(self, backend_family: str) -> str:
        return sha256_json({
            "task": self.task,
            "backend_family": backend_family,
            "images": self.image_hashes(),
            "stage_files": self.stage_hashes(),
            "render_params": self.render_params,
            "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "model": self.model,
            "effort": self.effort,
            "context_hash": self.context_hash,
        })


@dataclass
class ExtractionResponse:
    structured: dict[str, Any]
    envelope: dict[str, Any]
    backend: str
    backend_version: str
    model_requested: str
    model_resolved: str | None
    num_turns: int | None
    duration_s: float
    usage: dict[str, Any]
    cost_usd: float | None
    cache_key: str
    from_cache: bool = False


class Backend(Protocol):
    family: str

    def call(self, req: ExtractionRequest) -> ExtractionResponse: ...


class BackendError(Exception):
    """Base class. Failures are logged, never cached as successes."""


class TransientBackendError(BackendError):
    """Timeouts, overload, malformed envelope: retry with backoff."""


class SchemaInvalidError(BackendError):
    """The model answered but the output does not validate: one retry with the identical prompt."""


class BackendConfigError(BackendError):
    """The call cannot be trusted (image never read, permission denied): abort the run."""


class UsageLimitReached(BackendError):
    """Subscription limit hit: stop launching calls, let in-flight calls finish, exit 75."""

    def __init__(self, message: str, resets_at_text: str | None):
        super().__init__(message)
        self.resets_at_text = resets_at_text


class ReplayMiss(BackendError):
    """Strict replay: no recorded response for this cache key."""
