"""`lr mcp`: serve the contract, list it, mint a key."""

from __future__ import annotations

import json
import secrets
from pathlib import Path

import typer

mcp_app = typer.Typer(no_args_is_help=True, help="The MCP server: the tool contract of PRD §E.3 over stdio or HTTP.")


@mcp_app.command("serve")
def serve_cmd(
    stdio: bool = typer.Option(False, "--stdio", help="serve the client that launched this process, on stdin and stdout"),
    http: bool = typer.Option(False, "--http", help="serve streamable HTTP on its own port (when the API is not running)"),
    host: str = typer.Option("127.0.0.1", "--host", help="0.0.0.0 inside a container"),
    port: int = typer.Option(8788, "--port"),
    public_safe: bool = typer.Option(False, "--public-safe", help="never serve report text or a non-redistributable layer"),
    runs_dir: str = typer.Option("", "--runs-dir", help="where each session writes its run (default: data/runs)"),
) -> None:
    """Serve the tools, resources and prompts. One transport per process; the API mounts its own at /mcp."""
    from .server import build
    from .transport import serve_http, serve_stdio

    if stdio == http:
        raise typer.BadParameter("pick one transport: --stdio or --http")
    lr = build(public_safe=public_safe or None, runs_dir=Path(runs_dir) if runs_dir else None)
    if stdio:
        serve_stdio(lr)
    else:
        serve_http(lr, host=host, port=port, log=typer.echo)


@mcp_app.command("tools")
def tools_cmd(as_json: bool = typer.Option(False, "--json", help="the tools/list document, as a client sees it")) -> None:
    """The catalogue as served: name, kind, scope, and the read-only annotation, in the order a client lists it."""
    from . import TOOL_VERSION
    from .contract import CATALOGUE
    from .server import TOOLS

    if as_json:
        typer.echo(json.dumps([t.model_dump(by_alias=True, exclude_none=True) for t in TOOLS.values()], indent=1))
        return
    for name, spec in CATALOGUE.items():
        flag = "read-only" if spec.kind == "read" else spec.kind
        typer.echo(f"  {name:<20} {flag:<9} scope {spec.scope}")
    typer.echo(f"{len(CATALOGUE)} tools; serverInfo.version {TOOL_VERSION}")


@mcp_app.command("key")
def key_cmd(scopes: str = typer.Option("read", "--scopes", help="comma-separated: read, record, run")) -> None:
    """Mint a random API key and print the register entry for it. The key is printed once and never stored."""
    from .auth import ALL_SCOPES, KEY_VAR, KEYS_VAR

    wanted = [s.strip() for s in scopes.split(",") if s.strip()]
    bad = sorted(set(wanted) - ALL_SCOPES)
    if not wanted or bad:
        raise typer.BadParameter(f"scopes must be from {sorted(ALL_SCOPES)}, not {bad}")
    key = secrets.token_hex(16)
    typer.echo(f"  {KEYS_VAR}={key}:{','.join(wanted)}")
    typer.echo(f"  (append to an existing register with ';' between entries; a stdio client sets {KEY_VAR}={key})")
