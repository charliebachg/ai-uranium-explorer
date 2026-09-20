"""The MCP server: the tool contract of PRD §E.3, served to any client.

The same eight deterministic tools that serve the dashboard and the analyst (`prospect.tools`) are exposed
over the Model Context Protocol, behind a session handle that carries the leakage rules (`analyst.session`)
and a gate that binds every number to the id the tools gave it (`prospect.memo.check_claims`). Nothing here
computes a number, samples a model or bypasses a rule the loop already enforces: the server wraps, it does
not reimplement.

`auth` holds the API keys and scopes; `sessions` the handles, their expiry and the run manifest each one
writes; `contract` the catalogue, the `Val` shaping and the output schemas; `handlers` the tools; `holes` the
extraction crosscheck as a tool; `resources` and `prompts` what their names say; `server` the SDK server;
`transport` the streamable HTTP mount on the API and the stdio runner; `cli` the `lr mcp` commands.
"""

from __future__ import annotations

#: the tool-contract version a client sees as `serverInfo.version`
TOOL_VERSION = "prospect/tools/v2"
