"""The SDK server: the catalogue, the resources and the prompts behind the caller's key.

The low-level `Server` of the pinned SDK (2.x, where `FastMCP` became `MCPServer` over this same class) is
the right layer here, because the contract is written out already: `contract.py` holds every tool's input and
output schema, `resources.py` and `prompts.py` their lists, and a decorator that infers a schema from a
Python signature would only get in the way of a schema that says unknown is not absent. Each request handler
does three things: resolve who is calling (`auth`), refuse or filter by scope, and hand the work to a thread
(the tools are synchronous and hold the store), so the event loop stays free for the next request.

`serverInfo.version` is the tool-contract version; the tool list is built once from the catalogue and served
in catalogue order with a `ttlMs`, so a client or a prompt cache can hold it (PRD §E.3, transport).
"""

from __future__ import annotations

import os
from functools import partial
from pathlib import Path
from typing import Any, Callable, Mapping

import anyio
import mcp.types as types
from mcp.server.caching import CacheHint
from mcp.server.lowlevel.server import Server
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_REQUEST

from ..paths import PATHS
from ..prospect import tools as T
from . import TOOL_VERSION, prompts, resources
from .auth import KEY_VAR, KEYS_VAR, Keyring, Principal
from .contract import CATALOGUE
from .handlers import Handlers
from .sessions import SessionStore

SERVER_NAME = "legacy-reader"
TITLE = "Legacy Reader: the evidence record over MCP"
#: how long a client may hold the catalogue, the resource list and the prompt list
LIST_TTL_MS = 3_600_000
#: the environment flag that turns the public-safe build on; read by name
PUBLIC_VAR = "LR_MCP_PUBLIC"

INSTRUCTIONS = (
    "Public Saskatchewan exploration data over one 2 km grid, read through deterministic tools. Start with "
    "open_session(cell_id, purpose) and pass the session_id it returns to every other tool. Every number a tool "
    "returns is a value with an id in structuredContent and beside its id in the text; unknown (nobody measured "
    "it) and absent (measured, nothing there) arrive as distinct types. State a number only by citing its id, "
    "run check_claims on your claims before answering, and call abstain with a reason when the tools cannot "
    "answer. The handbook is lr://handbook and the criteria table lr://criteria. Scored and benchmark sessions "
    "serve out-of-fold scores only, mask the cell's own label and blind-list nearby files."
)

#: the tools as served, built once: the list is deterministic for the life of the contract version
TOOLS: dict[str, types.Tool] = {name: spec.tool() for name, spec in CATALOGUE.items()}


class LrServer:
    """Everything the request handlers close over: the keyring, the handlers, the session store."""

    def __init__(self, *, keyring: Keyring | None = None, environ: Mapping[str, str] | None = None,
                 public_safe: bool | None = None, runs_dir: Path | None = None,
                 tools: Mapping[str, Callable[..., Any]] | None = None, store: SessionStore | None = None,
                 handlers: Handlers | None = None) -> None:
        env = environ if environ is not None else os.environ
        self.environ = env
        self.keyring = keyring or Keyring.from_env(env)
        self.public_safe = bool(public_safe) if public_safe is not None else env.get(PUBLIC_VAR, "") == "1"
        self.runs_dir = runs_dir or PATHS.runs
        # `is not None`, not truthiness: an empty session store has a length of zero
        self.store = store if store is not None else SessionStore(
            runs_dir=self.runs_dir, tools=tools if tools is not None else T.REGISTRY)
        self.handlers = handlers if handlers is not None else Handlers(self.store, public_safe=self.public_safe)
        self.server: Server[Any] = Server(
            SERVER_NAME, version=TOOL_VERSION, title=TITLE, instructions=INSTRUCTIONS,
            cache_hints={m: CacheHint(ttl_ms=LIST_TTL_MS, scope="private")
                         for m in ("tools/list", "prompts/list", "resources/list", "resources/templates/list")},
            on_list_tools=self._list_tools, on_call_tool=self._call_tool,
            on_list_resources=self._list_resources, on_list_resource_templates=self._list_templates,
            on_read_resource=self._read_resource,
            on_list_prompts=self._list_prompts, on_get_prompt=self._get_prompt,
        )

    # ---------------------------------------------------------------- who is calling

    def principal(self, ctx: Any) -> Principal | None:
        """The caller: over HTTP the bearer key (or loopback, with no register); over stdio and in-process the
        local principal (or the key in `LR_MCP_KEY`). `ctx.request` is the Starlette request over HTTP and None
        on every other transport."""
        request = getattr(ctx, "request", None)
        headers = getattr(request, "headers", None)
        if request is not None and headers is not None:
            client = getattr(request, "client", None)
            return self.keyring.for_http(headers.get("authorization"), getattr(client, "host", None))
        return self.keyring.local(self.environ)

    def refusal(self, ctx: Any) -> str:
        request = getattr(ctx, "request", None)
        if request is not None and getattr(request, "headers", None) is not None:
            client = getattr(request, "client", None)
            return self.keyring.refusal(getattr(client, "host", None))
        return (f"{KEYS_VAR} is configured, so a stdio or in-process caller must name one of its keys in "
                f"{KEY_VAR}; without a register every local caller has every scope")

    def _require(self, ctx: Any) -> Principal:
        p = self.principal(ctx)
        if p is None:
            raise MCPError(code=INVALID_REQUEST, message=self.refusal(ctx))
        return p

    # ---------------------------------------------------------------- tools

    def tools_for(self, principal: Principal) -> list[types.Tool]:
        """The catalogue filtered by scope, in catalogue order (principle 9)."""
        return [TOOLS[name] for name, spec in CATALOGUE.items() if principal.allows(spec.scope)]

    async def _list_tools(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListToolsResult:
        return types.ListToolsResult(tools=self.tools_for(self._require(ctx)))

    async def _call_tool(self, ctx: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        principal = self.principal(ctx)
        if principal is None:
            # a tool result, not a protocol error: the client's model reads the reason and can act on it
            return types.CallToolResult(content=[types.TextContent(type="text", text=self.refusal(ctx))], is_error=True)
        return await anyio.to_thread.run_sync(partial(self.handlers.call, params.name, dict(params.arguments or {}), principal))

    # ---------------------------------------------------------------- resources and prompts

    async def _list_resources(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListResourcesResult:
        self._require(ctx)
        return types.ListResourcesResult(resources=list(resources.RESOURCES))

    async def _list_templates(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListResourceTemplatesResult:
        self._require(ctx)
        return types.ListResourceTemplatesResult(resource_templates=list(resources.TEMPLATES))

    async def _read_resource(self, ctx: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        self._require(ctx)
        contents = await anyio.to_thread.run_sync(partial(resources.read, str(params.uri), runs_dir=self.runs_dir))
        return types.ReadResourceResult(contents=[contents])

    async def _list_prompts(self, ctx: Any, params: types.PaginatedRequestParams | None) -> types.ListPromptsResult:
        self._require(ctx)
        return types.ListPromptsResult(prompts=prompts.list_prompts())

    async def _get_prompt(self, ctx: Any, params: types.GetPromptRequestParams) -> types.GetPromptResult:
        self._require(ctx)
        return prompts.get_prompt(params.name, dict(params.arguments or {}))

    # ---------------------------------------------------------------- lifecycle

    def close(self) -> None:
        """Every live session closed and its manifest finished: the transports call this on shutdown."""
        self.store.close_all()


def build(**kw: Any) -> LrServer:
    """The server the transports and the tests run; keyword arguments as `LrServer` takes them."""
    return LrServer(**kw)


__all__ = ["INSTRUCTIONS", "LIST_TTL_MS", "PUBLIC_VAR", "SERVER_NAME", "TOOLS", "LrServer", "build"]
