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

Since Phase 4c the agent behind this module is the interface agent (`legacy_reader.interface`, PRD §8.3): a
router classifies the question, a plan written in Python fetches the evidence for its kind, one answer call
on a cheap model says what was found, and the gate holds it. This module keeps the names the API, the MCP
session manifest and the persisted conversations already use: `Conversation`, `ask` with its signature,
`SYSTEM`, `STEP_SCHEMA`, `MAX_STEPS` and the prompt version.
"""

from __future__ import annotations

from typing import Any, Callable

from ..interface import agent as _agent
from ..interface.conversation import Conversation
from ..interface.loop import MAX_STEPS, PROMPT_VERSION, SCHEMA_VERSION, STEP_SCHEMA, SYSTEM


def ask(
    conv: Conversation, question: str, backend: Any, model: str = "claude-sonnet-5",
    effort: str = "medium", log: Callable[[str], None] = lambda _m: None,
    on_event: Callable[[dict[str, Any]], None] = lambda _e: None,
) -> dict[str, Any]:
    """One question, answered from the evidence record, with the gate applied before it is returned.

    `on_event` receives the turn's own steps as they happen: the route the agent took and its plan, which
    tool was called with what, when the gate is running, and the actions it recorded (an abstention, an
    insight, an analyst job), so a caller can show the work rather than a spinner."""
    return _agent.ask(conv, question, backend, model=model, effort=effort, log=log, on_event=on_event)


__all__ = ["Conversation", "MAX_STEPS", "PROMPT_VERSION", "SCHEMA_VERSION", "STEP_SCHEMA", "SYSTEM", "ask"]
