"""One model call, the way every call in the interface agent is made.

The request carries the staged evidence as files, the question inside its own hash (two questions over the
same evidence must never share a cache key) and the prompt and schema texts (B22). A backend that streams is
streamed so the panel can show the reply being written; one that cannot is called the way it expects.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from ..backends.base import ExtractionRequest
from ..backends.cache import streams
from ..ids import sha256_json, short

SCHEMA_VERSION = "1.0.0"

Event = Callable[[dict[str, Any]], None]


def request(*, task: str, stage: Path | None, system: str, prompt: str, schema: dict[str, Any],
            prompt_version: str, model: str, effort: str, salt: Any = None) -> ExtractionRequest:
    """The request for one call. `stage` None sends no files: the router classifies the question and needs
    none of the evidence, and a classification that carried the handbook and four tool results would cost
    more than the answer. `salt` joins the question in the context hash: the retry after a refusal carries a
    different one, so the refused answer is never served back from the cache."""
    return ExtractionRequest(
        task=task, images=(),
        stage_files=tuple(sorted((p, p.name) for p in stage.iterdir() if p.is_file())) if stage else (),
        system_prompt=system, user_prompt=prompt, schema=schema, schema_version=SCHEMA_VERSION,
        prompt_version=prompt_version, model=model, effort=effort,
        # The question is the input here, not the staged files. Without it two different questions over the
        # same evidence hash to the same key and the second one is answered from the first one's cache: a
        # stale answer that looks entirely convincing.
        context_hash=short(sha256_json([prompt, salt])),
    )


def call(backend: Any, req: ExtractionRequest, on_event: Event, step: int) -> tuple[dict[str, Any], float]:
    """The structured reply and what the call cost. Deltas go to the panel as they arrive."""
    def delta(piece: str) -> None:
        on_event({"type": "delta", "step": step, "text": piece})

    # only a backend that takes the callback is handed it; a TypeError from inside a call is a real error
    response = backend.call(req, on_delta=delta) if streams(backend) else backend.call(req)
    out = response.structured if isinstance(response.structured, dict) else {}
    return out, float(getattr(response, "cost_usd", 0.0) or 0.0)


__all__ = ["Event", "SCHEMA_VERSION", "call", "request"]
