"""The plain tool loop: what an unrouted question falls back to, capped at `MAX_STEPS`.

This is the conversational agent as it was before the router, kept whole: the model asks for a tool, the
runner executes it in Python and stages the result, and the model reads it on the next step. The one
addition is a third action, `abstain`, so a question the tools cannot answer ends in a recorded refusal with
a reason rather than a guess or a silent shrug. The gate is not here: the loop returns what the model
proposed, and `agent.ask` holds it to the same check as every other answer.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any, Callable

from ..backends.base import UsageLimitReached
from ..prospect import tools as T
from . import model as M
from .conversation import Conversation
from .sensitivity import TOOL as SENSITIVITY
from .sensitivity import sensitivity

PROMPT_VERSION = "prospect/chat/v4"
SCHEMA_VERSION = M.SCHEMA_VERSION
MAX_STEPS = 5
#: what `abstain` may be charged to; the same tuple the MCP server takes
ABSTAIN_REASONS: tuple[str, ...] = ("not_measured", "outside_grid", "no_value", "out_of_scope")

SYSTEM = """You answer questions about one 2 km cell of public Saskatchewan data, for someone assessing
unconformity-related uranium. You are a queryable interface to an evidence record, not a narrator.

Absolute rules:
1. Every number you state must come from a tool result in this session, and the claim that states it must list
   the value id of *that* number. This is checked mechanically; an answer that breaks it is not shown. Note
   that a count has its own id: a row's `observations_id` is not the same value as its `value_id`, and a
   threshold's id is not the same value as the membership it produced. Cite every id a claim needs, and split
   a claim carrying several numbers rather than hoping one id covers them all.
   A value id is a string from the `values` object of a tool result, and it always looks like
   `c:cell:0123_0045:d_conductor_m` or `c:crit:0123_0045:fault_proximity`. A filename is not a value id:
   citing `tool_03_criteria_breakdown.json` cites nothing and the answer will be refused.
2. You never compute anything yourself: no arithmetic, no distances, no conversions, no percentages you worked
   out. Ask a tool.
3. Unknown is not absent. If nobody measured something here, say that, and say how far away the nearest
   observation is if a tool gives you that.
4. You never recommend drilling, never estimate grade or tonnage, never give a probability that ore is present.
5. Criteria marked folklore may be named, never used as support.
6. If the question cannot be answered from the tools available, do not guess: reply with action "abstain" and
   the reason. `not_measured` when nobody measured it here; `outside_grid` when the cell or place is not on
   this grid; `no_value` when the store holds no value that would answer it; `out_of_scope` when it asks for
   something this record never says (a company's holdings, a grade or tonnage, where to drill). Say in the
   detail what would answer it.
7. A value in the expert tier (an id under `c:insight:`) is a geologist's own statement recorded in this
   conversation, not a measurement: cite it as such, and never present it as something the data shows.

Answer in plain prose, at most five short sentences, the answer first. One idea per sentence, no lists of
every criterion unless the question asks for them. A number that matters goes in its own claim with its
value id; do not repeat the claims in the prose."""

ANSWER_PROPERTIES: dict[str, Any] = {
    "answer": {
        "type": "object",
        "additionalProperties": False,
        "required": ["text", "claims"],
        "properties": {
            "text": {"type": "string"},
            "claims": {
                "type": "array",
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["text", "value_ids"],
                    "properties": {
                        "text": {"type": "string"},
                        "value_ids": {"type": "array", "items": {"type": "string"}},
                    },
                },
            },
            "caveats": {"type": "array", "items": {"type": "string"}},
            "cannot_answer": {"type": "boolean"},
        },
    },
    "abstain": {
        "type": "object",
        "additionalProperties": False,
        "required": ["reason"],
        "properties": {
            "reason": {"type": "string", "enum": list(ABSTAIN_REASONS)},
            "detail": {"type": "string"},
        },
    },
}

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["call_tool", "answer", "abstain"]},
        "reasoning": {"type": "string"},
        "tool": {"type": "string"},
        "args": {"type": "object", "additionalProperties": True},
        **ANSWER_PROPERTIES,
    },
}

#: the tools the loop may call: the registry's eight, plus the sensitivity the router's plan runs
TOOL_HELP: dict[str, str] = {
    **T.TOOL_HELP,
    SENSITIVITY: f"{SENSITIVITY}(cell_id) - which unknown criterion, measured and met, would move the criteria "
                 "score most; computed from the weights and memberships, every figure with an id",
}


def call_tool(tool: str, args: dict[str, Any]) -> T.ToolResult:
    """One tool call: the registry's own, or the sensitivity, which lives with the interface agent."""
    if tool == SENSITIVITY:
        cell = args.get("cell_id")
        if not isinstance(cell, str) or not cell:
            raise T.ToolError(f"{SENSITIVITY} needs a cell_id")
        return sensitivity(cell)
    return T.call(tool, args)


@dataclass
class Outcome:
    """What a run of the loop or an answer call came back with: an answer or an abstention, never both."""

    answer: dict[str, Any] | None = None
    abstain: dict[str, Any] | None = None
    spent: float = 0.0
    problems: list[str] = field(default_factory=list)
    usage_limited: bool = False


def _prompt(conv: Conversation, question: str, step: int) -> str:
    listing = "\n".join(f"  {c['file']}  <- {c['tool']}({json.dumps(c['args'])})" for c in conv.calls)
    left = MAX_STEPS - step - 1
    ask = (f'You may call {left} more tool(s). Reply with action "call_tool" to ask for one, "answer" '
           f'when you can answer from what you have, or "abstain" when the tools cannot answer it. '
           if left > 0 else 'Reply with action "answer" using what is already staged, or "abstain". ')
    return f"""Cell {conv.cell_id}.

The question: {question}

Earlier in this conversation:
{conv.transcript()}

Read these first if you have not:
  {{STAGE_DIR}}/handbook.md    what the record supports and what it does not
  {{STAGE_DIR}}/criteria.toml  the criteria, thresholds, status and caveats
Evidence already staged:
{listing}

Tools you may call:
{chr(10).join("  " + h for h in TOOL_HELP.values())}

{ask}Every number needs the value id it came from."""


def run(conv: Conversation, question: str, backend: Any, model: str, effort: str,
        on_event: M.Event, log: Callable[[str], None]) -> Outcome:
    """Up to `MAX_STEPS` model calls; each may call one tool, answer, or abstain."""
    assert conv.stage is not None
    out = Outcome()
    for step in range(MAX_STEPS):
        on_event({"type": "thinking", "step": step + 1, "of": MAX_STEPS})
        req = M.request(task="prospect_chat", stage=conv.stage, system=SYSTEM,
                        prompt=_prompt(conv, question, step), schema=STEP_SCHEMA,
                        prompt_version=PROMPT_VERSION, model=model, effort=effort)
        try:
            reply, cost = M.call(backend, req, on_event, step + 1)
        except UsageLimitReached as limit:
            out.problems.append(f"usage limit: {limit}")
            out.usage_limited = True
            return out
        out.spent += cost
        # the model says why before it says what; showing that is the difference between watching an agent
        # work and watching a spinner
        if reply.get("reasoning"):
            on_event({"type": "reasoning", "step": step + 1, "text": str(reply["reasoning"])})
        action = reply.get("action")
        if action == "call_tool":
            tool, args = str(reply.get("tool")), dict(reply.get("args") or {})
            reason = str(reply.get("reasoning") or "")
            try:
                result = conv.record(call_tool(tool, args))
                log(f"    tool: {tool}({json.dumps(args)})")
                on_event({"type": "tool", "tool": tool, "args": args, "reasoning": reason,
                          "rows": len(result.rows), "values": len(result.values)})
            except T.ToolError as err:
                (conv.stage / f"tool_error_{step}.json").write_text(json.dumps({"error": str(err)}))
                log(f"    tool error: {err}")
                on_event({"type": "tool_error", "tool": tool, "args": args, "error": str(err)})
            continue
        if action == "abstain":
            out.abstain = dict(reply.get("abstain") or {})
            return out
        out.answer = dict(reply.get("answer") or {})
        return out
    out.problems.append("ran out of steps without answering")
    return out


__all__ = ["ABSTAIN_REASONS", "ANSWER_PROPERTIES", "MAX_STEPS", "Outcome", "PROMPT_VERSION", "SCHEMA_VERSION",
           "STEP_SCHEMA", "SYSTEM", "TOOL_HELP", "call_tool", "run"]
