"""A conversation bound to one cell: ask about the ground, the agent reads what it needs.

This is the part MineTRACE calls the assistant, and the principle it states is the one worth copying: the
assistant is "a queryable interface to the interpretable model rather than an independent narrator". It answers
from the same evidence record the map and the panel are drawn from, so the three views cannot disagree.

Two differences from that design, both deliberate:

* **The grounding is enforced, not requested.** MineTRACE instructs its assistant to report only tool-computed
  values, and one response in 150 still carried a fabricated number. Here every number in an answer is checked
  against the values the tools returned in this session, and an answer that fails is not shown.
* **Missing is a first-class answer.** A cell with no lake has no lake-sediment reading, and the agent is
  required to say so rather than treat silence as a low value.
"""

from __future__ import annotations

import datetime as dt
import json
import tempfile
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from ..backends.base import ExtractionRequest, UsageLimitReached
from ..backends.cache import streams
from ..ids import sha256_json, short
from . import tools as T
from .memo import CRITERIA_FILE, HANDBOOK, Session, check_claims, quotable

PROMPT_VERSION = "prospect/chat/v2"
SCHEMA_VERSION = "1.0.0"
MAX_STEPS = 5

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
6. If the question cannot be answered from the tools available, say so plainly and name what would answer it.

Answer in plain prose. Short is fine. When a number matters, put it in its own claim with its value id."""

STEP_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["action"],
    "properties": {
        "action": {"type": "string", "enum": ["call_tool", "answer"]},
        "reasoning": {"type": "string"},
        "tool": {"type": "string"},
        "args": {"type": "object", "additionalProperties": True},
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
    },
}


@dataclass
class Conversation:
    """One chat about one cell. Holds the evidence record the answers are checked against."""

    cell_id: str
    conversation_id: str = field(default_factory=lambda: uuid.uuid4().hex[:12])
    stage: Path | None = None
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    context: str = ""
    turns: list[dict[str, Any]] = field(default_factory=list)
    calls: list[dict[str, Any]] = field(default_factory=list)
    cost_usd: float = 0.0

    def open(self) -> "Conversation":
        """Stage the standing material and the opening evidence, once per conversation."""
        if self.stage is not None:
            return self
        self.stage = Path(tempfile.mkdtemp(prefix="lr_chat_"))
        (self.stage / "handbook.md").write_text(HANDBOOK.read_text())
        (self.stage / "criteria.toml").write_text(CRITERIA_FILE.read_text())
        for tool in ("cell_scores", "cell_features", "criteria_breakdown", "label_context"):
            self.record(T.call(tool, {"cell_id": self.cell_id}))
        return self

    def record(self, result: T.ToolResult) -> T.ToolResult:
        assert self.stage is not None
        self.values |= result.values
        payload = json.dumps(result.as_json(), indent=1)
        self.context += "\n" + quotable(result.as_json())
        name = f"tool_{len(self.calls) + 1:02d}_{result.tool}.json"
        (self.stage / name).write_text(payload)
        self.calls.append({"tool": result.tool, "args": result.args, "file": name,
                           "rows": len(result.rows), "values": len(result.values)})
        return result

    def transcript(self) -> str:
        lines = []
        for t in self.turns[-6:]:
            lines.append(f"  user: {t['question']}")
            lines.append(f"  you:  {t.get('text', '(not shown: failed the evidence check)')}")
        return "\n".join(lines) or "  (this is the first question)"


def _prompt(conv: Conversation, question: str, step: int) -> str:
    listing = "\n".join(f"  {c['file']}  <- {c['tool']}({json.dumps(c['args'])})" for c in conv.calls)
    left = MAX_STEPS - step - 1
    ask = (f'You may call {left} more tool(s). Reply with action "call_tool" to ask for one, or "answer" '
           f'when you can answer from what you have. '
           if left > 0 else 'Reply with action "answer" using what is already staged. ')
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
{chr(10).join("  " + h for h in T.TOOL_HELP.values())}

{ask}Every number needs the value id it came from."""


def ask(
    conv: Conversation, question: str, backend: Any, model: str = "claude-sonnet-5",
    effort: str = "medium", log: Callable[[str], None] = lambda _m: None,
    on_event: Callable[[dict[str, Any]], None] = lambda _e: None,
) -> dict[str, Any]:
    """One question, answered from the evidence record, with the gate applied before it is returned.

    `on_event` receives the loop's own steps as they happen — which tool was called with what, and when the
    gate is running — so a caller can show the work rather than a spinner. Watching an agent decide it needs
    the criteria breakdown, then the label context, is most of what makes it legible; a progress bar teaches
    nobody anything.
    """
    conv.open()
    on_event({"type": "opened", "tools": [c["tool"] for c in conv.calls]})
    assert conv.stage is not None
    answer: dict[str, Any] | None = None
    spent = 0.0
    calls_before = len(conv.calls)

    for step in range(MAX_STEPS):
        on_event({"type": "thinking", "step": step + 1, "of": MAX_STEPS})
        prompt = _prompt(conv, question, step)
        req = ExtractionRequest(
            task="prospect_chat",
            images=(),
            stage_files=tuple(sorted((p, p.name) for p in conv.stage.iterdir() if p.is_file())),
            system_prompt=SYSTEM,
            user_prompt=prompt,
            schema=STEP_SCHEMA,
            schema_version=SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            model=model,
            effort=effort,
            # The question is the input here, not the staged files. Without it two different questions over
            # the same evidence hash to the same key and the second one is answered from the first one's
            # cache: a stale answer that looks entirely convincing.
            context_hash=short(sha256_json(prompt)),
        )
        try:
            # only the API backend can stream; the CLI one is called the way it expects, and the panel falls
            # back to per-step reasoning. Asked, not caught: a TypeError from inside a call is a real error.
            def delta(piece: str) -> None:
                on_event({"type": "delta", "step": step + 1, "text": piece})

            response = (
                backend.call(req, on_delta=delta) if streams(backend) else backend.call(req)
            )
        except UsageLimitReached as limit:
            turn = {"question": question, "text": None, "published": False,
                    "problems": [f"usage limit: {limit}"], "usage_limited": True}
            conv.turns.append(turn)
            return turn
        spent += float(getattr(response, "cost_usd", 0.0) or 0.0)
        out = response.structured
        # the model says why before it says what; showing that is the difference between watching an agent
        # work and watching a spinner
        if out.get("reasoning"):
            on_event({"type": "reasoning", "step": step + 1, "text": str(out["reasoning"])})
        if out.get("action") == "call_tool":
            tool, args = str(out.get("tool")), dict(out.get("args") or {})
            reason = str(out.get("reasoning") or "")
            try:
                result = conv.record(T.call(tool, args))
                log(f"    tool: {tool}({json.dumps(args)})")
                on_event({"type": "tool", "tool": tool, "args": args, "reasoning": reason,
                          "rows": len(result.rows), "values": len(result.values)})
            except T.ToolError as err:
                (conv.stage / f"tool_error_{step}.json").write_text(json.dumps({"error": str(err)}))
                log(f"    tool error: {err}")
                on_event({"type": "tool_error", "tool": tool, "args": args, "error": str(err)})
            continue
        answer = out.get("answer") or {}
        break

    conv.cost_usd += spent
    if answer is None:
        turn = {"question": question, "text": None, "published": False,
                "problems": ["ran out of steps without answering"], "cost_usd": round(spent, 4)}
        conv.turns.append(turn)
        return turn

    on_event({"type": "checking", "claims": len(answer.get("claims") or [])})
    claims = list(answer.get("claims") or [])
    problems = check_claims(claims, conv.values, context=conv.context)
    # the prose is held to the same rule as the claims: a number in the summary must also be backed
    problems += check_claims([{"text": str(answer.get("text") or ""),
                               "value_ids": [v for c in claims for v in (c.get("value_ids") or [])]}],
                             conv.values, context=conv.context)
    turn = {
        "question": question,
        "text": str(answer.get("text") or "") if not problems else None,
        "claims": claims if not problems else [],
        "caveats": list(answer.get("caveats") or []),
        "cannot_answer": bool(answer.get("cannot_answer")),
        "published": not problems,
        "problems": problems,
        "tools_used": [c["tool"] for c in conv.calls[calls_before:]],
        "cost_usd": round(spent, 4),
        "asked_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
    conv.turns.append(turn)
    return turn
