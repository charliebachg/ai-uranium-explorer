"""Prompts: the six roles, each built from the prompt text the in-house loops already use, never copied.

`proponent`, `skeptic` and `adjudicator` are the panel's (`prospect.memo.SYSTEM` and `ROLE_PROMPT`);
`analyst.executor` is the staged loop's node protocol (`analyst.prompts.default_executor_system`, closed-book,
the handbook's frame and the criteria lines inside it); `analyst.verifier` is the skeptic's brief
(`analyst.prompts.verifier_system`); `interface.router` is the chat agent's instruction
(`prospect.chat.SYSTEM`) with the routing the interface agent adds. Every prompt takes `cell_id` and
`session_id` and ends with the same footer: read through the tools with that handle, cite ids, run
`check_claims` before answering, `abstain` when the tools cannot answer.
"""

from __future__ import annotations

import mcp.types as types
from mcp.shared.exceptions import MCPError
from mcp_types import INVALID_PARAMS

from ..analyst import prompts as AP
from ..prospect import chat as CHAT
from ..prospect import memo

ARGUMENTS: list[types.PromptArgument] = [
    types.PromptArgument(name="cell_id", required=True,
                         description="the cell the session was opened on (its bench id in a benchmark session)"),
    types.PromptArgument(name="session_id", required=True, description="the handle open_session returned"),
]

DESCRIPTIONS: dict[str, str] = {
    "proponent": "The panel's proponent: the strongest honest case that the cell deserves a closer look, or that it does not.",
    "skeptic": "The panel's skeptic: attack the case for effort artefacts, proximity, unknowns read as met, thin coverage, folklore and unbacked numbers.",
    "adjudicator": "The panel's adjudicator: rule, separate unknown from absent, name the one observation that would change the verdict.",
    "analyst.executor": "The staged analyst's executor: decide one criterion from staged tool results, closed-book, as one node with its value ids.",
    "analyst.verifier": "The staged analyst's verifier: the skeptic's brief over a whole chain, naming the faulty nodes.",
    "interface.router": "The interface agent: a queryable interface to the evidence record that answers, abstains or records an insight.",
}
NAMES: tuple[str, ...] = tuple(DESCRIPTIONS)


def footer(cell_id: str, session_id: str) -> str:
    return (f"You work through the MCP tools of this server with session_id {session_id}, opened on cell {cell_id}. "
            "Read the evidence with the read tools (cell_features, cell_scores, criteria_breakdown, label_context, "
            "nearby, coverage, crosscheck, retrieve); the handbook is the resource lr://handbook and the criteria "
            "table lr://criteria. Every number you state must cite, in its claim's value_ids, the id the tool "
            "returned it under. Call check_claims on your claims before you answer and rewrite or drop any claim it "
            "rejects; call abstain, with its reason, when the tools cannot answer. You never compute a number.")


def _panel(role: str, cell_id: str, session_id: str) -> str:
    text = (memo.ROLE_PROMPT[role]
            .replace("{STAGE_DIR}/proponent.json", "the proponent's memo earlier in this conversation")
            .replace("{STAGE_DIR}/skeptic.json", "the skeptic's memo earlier in this conversation"))
    return "\n\n".join([memo.SYSTEM, f"Cell {cell_id}. You are the {role}.", text, footer(cell_id, session_id)])


def text_for(name: str, cell_id: str, session_id: str) -> str:
    if name in memo.ROLES:
        return _panel(name, cell_id, session_id)
    if name == "analyst.executor":
        return "\n\n".join([
            AP.default_executor_system(),
            f"Cell {cell_id}, session {session_id}. Here the harness is you: stage a criterion's evidence by calling "
            "the tools for it, then return its node as JSON (criterion, status, strength, value_ids, text, "
            "expert_ids, unknown_reason). One criterion at a time; a value id is a string from a tool result's "
            "values.",
            footer(cell_id, session_id),
        ])
    if name == "analyst.verifier":
        return "\n\n".join([
            AP.verifier_system(),
            AP.verifier_user(f"(read the chain from the resource lr://cell/{cell_id}/chains, one node per line)",
                             f"(call cell_scores with session_id {session_id} for the effort null and its ids)"),
            footer(cell_id, session_id),
        ])
    if name == "interface.router":
        return "\n\n".join([
            CHAT.SYSTEM,
            f"Cell {cell_id}, session {session_id}. Route each question: answer it from the tools when they can "
            "answer it; call abstain with its reason when they cannot; when the user states an observation of "
            "their own, confirm it with them and call record_insight so it is kept as an expert-tier value rather "
            "than folded into your answer.",
            footer(cell_id, session_id),
        ])
    raise MCPError(code=INVALID_PARAMS, message=f"no prompt named {name!r}; available: {', '.join(NAMES)}")


def list_prompts() -> list[types.Prompt]:
    return [types.Prompt(name=name, title=name, description=desc, arguments=ARGUMENTS)
            for name, desc in DESCRIPTIONS.items()]


def get_prompt(name: str, arguments: dict[str, str] | None) -> types.GetPromptResult:
    args = dict(arguments or {})
    missing = [a.name for a in ARGUMENTS if not str(args.get(a.name) or "").strip()]
    if missing:
        raise MCPError(code=INVALID_PARAMS, message=f"prompt {name!r} needs {', '.join(missing)}")
    if name not in DESCRIPTIONS:
        raise MCPError(code=INVALID_PARAMS, message=f"no prompt named {name!r}; available: {', '.join(NAMES)}")
    text = text_for(name, str(args["cell_id"]).strip(), str(args["session_id"]).strip())
    return types.GetPromptResult(description=DESCRIPTIONS[name],
                                 messages=[types.PromptMessage(role="user", content=types.TextContent(type="text", text=text))])
