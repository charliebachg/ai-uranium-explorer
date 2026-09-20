"""The two transports (PRD §E.3): streamable HTTP inside the API process, and stdio for a local client.

Over HTTP the server is one route on the FastAPI app, `/mcp`, served by the SDK's session manager in its
stateless, JSON-response mode: every request is complete in itself, which is what a handle-carrying contract
wants (principle 6: the session is `open_session`'s handle, never the transport's), and a JSON reply is what
the plainest client can read. The manager runs for the life of the process, so the mount wraps the app's
lifespan rather than trusting a sub-application's, which Starlette does not start. `lr mcp serve --http`
serves the same route on its own Starlette app when the API is not running; `--stdio` serves a client that
launched this process and owns its stdin and stdout, which is how Claude Code and Cursor connect.

Local-only by default (principle 9): with no key register the HTTP route answers loopback addresses only,
and the SDK's DNS-rebinding check is turned on with the loopback hosts allowed, so a page in a browser on the
same machine cannot reach the route under another name. With a register configured the check is off, because
the deployment then sits behind whatever host it was given and every caller presents a key.
"""

from __future__ import annotations

from contextlib import asynccontextmanager
from typing import Any, AsyncIterator, Callable

import anyio
from starlette.applications import Starlette
from starlette.responses import PlainTextResponse
from starlette.routing import Route

from mcp.server.stdio import stdio_server
from mcp.server.streamable_http_manager import StreamableHTTPASGIApp, StreamableHTTPSessionManager
from mcp.server.transport_security import TransportSecuritySettings

from .server import LrServer

MCP_PATH = "/mcp"
LOOPBACK_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
LOOPBACK_ORIGINS = ["http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]


def security_for(lr: LrServer) -> TransportSecuritySettings | None:
    if lr.keyring.configured:
        return None
    return TransportSecuritySettings(enable_dns_rebinding_protection=True, allowed_hosts=LOOPBACK_HOSTS,
                                     allowed_origins=LOOPBACK_ORIGINS)


def session_manager(lr: LrServer) -> StreamableHTTPSessionManager:
    return StreamableHTTPSessionManager(app=lr.server, json_response=True, stateless=True,
                                        security_settings=security_for(lr))


class Endpoint:
    """The ASGI endpoint for the route: hands each request to the session manager of the current lifespan.

    The SDK's manager runs once per instance, and an app's lifespan can run more than once in a process (a
    test client entering it twice, a reload), so the manager is made fresh on each entry and the route holds
    only a reference to the one that is running. Outside a lifespan the route answers 503 rather than raising."""

    def __init__(self, lr: LrServer) -> None:
        self.lr = lr
        self.manager: StreamableHTTPSessionManager | None = None

    @asynccontextmanager
    async def running(self) -> AsyncIterator[None]:
        manager = session_manager(self.lr)
        async with manager.run():
            self.manager = manager
            try:
                yield
            finally:
                self.manager = None
                self.lr.close()

    async def __call__(self, scope: Any, receive: Any, send: Any) -> None:
        if self.manager is None:
            await PlainTextResponse("the MCP transport is not running: the app's lifespan has not started",
                                    status_code=503)(scope, receive, send)
            return
        await StreamableHTTPASGIApp(self.manager)(scope, receive, send)


def mount(app: Any, lr: LrServer, path: str = MCP_PATH) -> Endpoint:
    """The MCP route on a FastAPI (or Starlette) app, and the transport's lifetime tied to the app's.

    Added as a plain Starlette route rather than a sub-application, so `POST /mcp` reaches the transport
    without a trailing-slash redirect a client would not follow. The app's existing lifespan still runs; the
    session manager runs inside it, and every live MCP session is closed on shutdown so its manifest is
    finished. Declare the route before any catch-all."""
    endpoint = Endpoint(lr)
    app.add_route(path, endpoint)
    previous = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Any) -> AsyncIterator[Any]:
        async with previous(a) as state, endpoint.running():
            yield state

    app.router.lifespan_context = lifespan
    app.state.mcp = lr
    return endpoint


def http_app(lr: LrServer, path: str = MCP_PATH) -> Starlette:
    """A Starlette app of one route, for `lr mcp serve --http` when the API process is not running."""
    endpoint = Endpoint(lr)

    @asynccontextmanager
    async def lifespan(_app: Starlette) -> AsyncIterator[None]:
        async with endpoint.running():
            yield

    return Starlette(routes=[Route(path, endpoint=endpoint)], lifespan=lifespan)


def serve_http(lr: LrServer, host: str = "127.0.0.1", port: int = 8788, path: str = MCP_PATH,
               log: Callable[[str], None] = print) -> None:
    import uvicorn

    log(f"  MCP over streamable HTTP at http://{host}:{port}{path}  (tool contract {lr.server.version}"
        f"{', public-safe' if lr.public_safe else ''}, "
        f"{'keys from the register' if lr.keyring.configured else 'local clients only'})")
    uvicorn.run(http_app(lr, path), host=host, port=port, log_level="warning")


async def run_stdio(lr: LrServer) -> None:
    """One client on stdin and stdout, until it closes the pipe. The SDK diverts fd 1 to stderr while serving,
    so a stray print from a tool cannot land on the wire."""
    async with stdio_server() as (read_stream, write_stream):
        try:
            await lr.server.run(read_stream, write_stream, lr.server.create_initialization_options())
        finally:
            lr.close()


def serve_stdio(lr: LrServer) -> None:
    anyio.run(run_stdio, lr)


__all__ = ["MCP_PATH", "Endpoint", "http_app", "mount", "run_stdio", "security_for", "serve_http", "serve_stdio",
           "session_manager"]
