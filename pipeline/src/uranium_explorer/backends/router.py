"""One backend that sends each request to the adapter its model id belongs to.

An analyst arm runs three roles, and a cheap executor on OpenRouter beside an Opus verifier on the CLI is the
pairing the evidence favours. The harness builds one backend per arm, so this is where the split lives: the
model id on the request decides, the cache keys by the adapter that answered (each adapter's own family), and
spend lands on that adapter's ledger family. Nothing about a request changes on the way through.
"""

from __future__ import annotations

from typing import Any, Callable

from .base import ExtractionRequest, ExtractionResponse

Route = tuple[Callable[[str], bool], Any]


class RoutedBackend:
    family = "routed"

    def __init__(self, routes: list[Route], default: Any) -> None:
        self.routes = list(routes)
        self.default = default

    def route(self, req: ExtractionRequest) -> Any:
        """The adapter for this request's model: the first route whose test accepts the id, else the default."""
        for accepts, backend in self.routes:
            if accepts(req.model):
                return backend
        return self.default

    def call(self, req: ExtractionRequest, on_delta: Callable[[str], None] | None = None) -> ExtractionResponse:
        backend = self.route(req)
        if on_delta is not None:
            from .cache import streams

            if streams(backend):
                return backend.call(req, on_delta=on_delta)
        return backend.call(req)


class MissingCliBackend:
    """The default route on a host without the `claude` binary (a container, a CI runner): a request that
    reaches it is refused with the fix named, and nothing fails at start over a route nothing may take."""

    family = "claude_cli"

    def call(self, req: Any) -> Any:
        from .base import BackendConfigError

        raise BackendConfigError(
            f"model {req.model!r} routes to the Claude CLI, which this host does not have; name a vendor/model "
            "id so the call goes to OpenRouter, or install the CLI and sign in"
        )


def cli_or_missing(make_cli: Callable[[], Any]) -> Any:
    """The CLI adapter where its binary exists, else the refusing stand-in. The probe is the binary's
    presence, not its login: a signed-out CLI still fails at call time with its own message."""
    import shutil

    return make_cli() if shutil.which("claude") else MissingCliBackend()
