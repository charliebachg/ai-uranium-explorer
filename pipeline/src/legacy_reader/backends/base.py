"""Backend interface and error taxonomy shared by the claude CLI, replay and (later) API adapters."""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol

from ..ids import sha256_bytes, sha256_file, sha256_json

#: the three text hashes the cache key gained with B22; the old key was the same dictionary without them
HASH_KEYS = ("system_hash", "user_hash", "schema_hash")


def hash_text(text: str) -> str:
    return sha256_bytes(text.encode("utf-8"))


def hash_schema(schema: dict[str, Any]) -> str:
    """The schema hashed as canonical JSON, so key order and whitespace cannot change the key."""
    return sha256_bytes(json.dumps(schema, sort_keys=True, separators=(",", ":")).encode("utf-8"))


def cache_key_fields(*, task: str, backend_family: str, image_hashes: list[str], stage_hashes: list[list[str]],
                     render_params: dict[str, Any], prompt_version: str, schema_version: str, model: str,
                     effort: str, context_hash: str, system_hash: str, user_hash: str,
                     schema_hash: str) -> dict[str, Any]:
    """The dictionary a cache key is the hash of, laid out in one place so a re-key of old records
    (`runtime.rekey`) builds exactly the key a live request computes."""
    return {
        "task": task,
        "backend_family": backend_family,
        "images": image_hashes,
        "stage_files": stage_hashes,
        "render_params": render_params,
        "prompt_version": prompt_version,
        "schema_version": schema_version,
        "model": model,
        "effort": effort,
        "context_hash": context_hash,
        "system_hash": system_hash,
        "user_hash": user_hash,
        "schema_hash": schema_hash,
    }


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

    def system_hash(self) -> str:
        return hash_text(self.system_prompt)

    def user_hash(self) -> str:
        """The user prompt as given, `{STAGE_DIR}` placeholder and all: the staged path is outside the key."""
        return hash_text(self.user_prompt)

    def schema_hash(self) -> str:
        return hash_schema(self.schema)

    def cache_key(self, backend_family: str) -> str:
        """Everything the answer depends on, and nothing it does not: the versions *and* the texts of the
        prompts and schema (B22), so an edited prompt is never served an older prompt's answer."""
        return sha256_json(cache_key_fields(
            task=self.task, backend_family=backend_family,
            image_hashes=self.image_hashes(), stage_hashes=self.stage_hashes(),
            render_params=self.render_params, prompt_version=self.prompt_version,
            schema_version=self.schema_version, model=self.model, effort=self.effort,
            context_hash=self.context_hash, system_hash=self.system_hash(), user_hash=self.user_hash(),
            schema_hash=self.schema_hash(),
        ))


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
