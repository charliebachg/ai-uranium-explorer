"""One backend that sends each request to the API its model id belongs to. API first.

`auto` is the shape a public artifact runs on: every route is a pay-per-token API, reached with a key in
`.env`, and nothing depends on a tool installed or signed in on the host. A `vendor/model` id goes to
OpenRouter; a bare `claude-*` id goes to OpenRouter's Anthropic listing under `anthropic/<id>`, so an arm file
that names a Claude model runs unchanged; a `gpt-*` id goes to the OpenAI adapter. Anything else is refused
at call time with the accepted shapes named. The Claude CLI is never a route here: it stays the explicit
`--backend claude`, a developer's option that needs a local login.

The harness builds one backend per arm, so this is where an arm's split between roles lives: the model id on
the request decides, the cache keys by the adapter that answered (each adapter's own family), and spend lands
on that adapter's ledger family. Adapters are built on first use, so a service with no key configured still
starts and serves the record; only a call that needs the missing key fails, with that key named.
"""

from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable

from .base import BackendConfigError, ExtractionRequest, ExtractionResponse

#: a route: a test on the model id, and the adapter (or a zero-argument factory that builds it on first use)
Route = tuple[Callable[[str], bool], Any]

OPENAI_PREFIXES = ("gpt-", "o1", "o3", "o4", "text-", "chatgpt-")
ANTHROPIC_LISTING = "anthropic/"


def is_claude_model(model: str) -> bool:
    """A bare Anthropic id as the arm files write it (`claude-sonnet-5`), not yet a vendor/model id."""
    return model.startswith("claude-") and "/" not in model


def is_openai_model(model: str) -> bool:
    return model.startswith(OPENAI_PREFIXES) and "/" not in model


def anthropic_listing(model: str) -> str:
    """The OpenRouter id for a bare Claude model: the Anthropic listing carries the same names."""
    return f"{ANTHROPIC_LISTING}{model}" if is_claude_model(model) else model


class RoutedBackend:
    family = "routed"

    def __init__(self, routes: list[Route], default: Any = None,
                 rename: Callable[[str], str] | None = None) -> None:
        self.routes = list(routes)
        self.default = default
        self.rename = rename
        self._built: dict[int, Any] = {}

    def _adapter(self, key: int, entry: Any) -> Any:
        """The adapter for a route: built once if the route holds a factory, else the adapter as given."""
        if key in self._built:
            return self._built[key]
        adapter = entry() if callable(entry) and not hasattr(entry, "call") else entry
        self._built[key] = adapter
        return adapter

    def route(self, req: ExtractionRequest) -> Any:
        """The adapter for this request's model: the first route whose test accepts the id, else the default;
        no default means the model has no API route, which is the caller's mistake to fix."""
        for i, (accepts, entry) in enumerate(self.routes):
            if accepts(req.model):
                return self._adapter(i, entry)
        if self.default is None:
            raise BackendConfigError(
                f"no API route for model {req.model!r}: name a vendor/model id (OpenRouter), a claude-* id "
                "(Anthropic through OpenRouter) or a gpt-* id (OpenAI); the Claude CLI is only --backend claude"
            )
        return self._adapter(-1, self.default)

    def request_for(self, req: ExtractionRequest) -> ExtractionRequest:
        """The request as the adapter must see it: the model id renamed where the route requires it."""
        if self.rename is None:
            return req
        model = self.rename(req.model)
        return req if model == req.model else replace(req, model=model)

    def call(self, req: ExtractionRequest, on_delta: Callable[[str], None] | None = None) -> ExtractionResponse:
        backend = self.route(req)
        sent = self.request_for(req)
        if on_delta is not None:
            from .cache import streams

            if streams(backend):
                return backend.call(sent, on_delta=on_delta)
        return backend.call(sent)


def api_first(timeout_s: float = 180.0, max_output_tokens: int | None = None,
              anthropic_timeout_s: float | None = None) -> RoutedBackend:
    """The `auto` backend: OpenRouter for vendor/model ids, OpenRouter's Anthropic listing for bare claude-*
    ids (their own adapter, so a reader's timeout and a second family's output cap stay apart), OpenAI for
    gpt-* ids; every adapter built on first use, every other id refused with the fix."""
    from .openrouter import OpenRouterBackend, is_openrouter_model

    def vendor() -> Any:
        return OpenRouterBackend(timeout_s=timeout_s, max_output_tokens=max_output_tokens)

    def anthropic() -> Any:
        return OpenRouterBackend(timeout_s=anthropic_timeout_s or timeout_s)

    def openai() -> Any:
        from .openai_api import OpenAIBackend

        return OpenAIBackend()

    return RoutedBackend(
        [(is_openrouter_model, vendor), (is_claude_model, anthropic), (is_openai_model, openai)],
        default=None, rename=anthropic_listing,
    )


__all__ = ["ANTHROPIC_LISTING", "RoutedBackend", "anthropic_listing", "api_first", "is_claude_model", "is_openai_model"]
