"""A conversation bound to one cell: the evidence record every answer is checked against.

Moved here from `prospect.chat` when the interface agent arrived; the shape is the one the API's registry and
the persisted turns already rely on (`cell_id`, `conversation_id`, `values`, `calls`, `turns`, `cost_usd`),
with what the agent's actions add: the abstentions it recorded, the insights it wrote and may now cite (B19),
the analyst jobs it submitted, and the label of whoever is asking.
"""

from __future__ import annotations

import json
import tempfile
import uuid
from dataclasses import dataclass, field
from functools import partial
from pathlib import Path
from typing import Any, Callable

from ..prospect import tools as T
from ..prospect.memo import CRITERIA_FILE, HANDBOOK, quotable
from ..store import connect

#: who recorded an insight when no principal is known: the local dashboard, on the person's own machine
LOCAL = "local"


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
    #: the caller's label: the API's principal when auth is in front of it, `local` otherwise
    requested_by: str = LOCAL
    #: where `record_insight` writes: the live store read-write by default (the API process is one-mode), a
    #: temporary copy in a test or a smoke run; None refuses to write at all
    insight_store: Callable[[], Any] | None = field(default_factory=lambda: partial(connect, None, False), repr=False)
    #: what the actions recorded, in the shapes the MCP handlers append (the same functions run here)
    abstentions: list[dict[str, Any]] = field(default_factory=list)
    insights: list[dict[str, Any]] = field(default_factory=list)
    jobs: list[dict[str, Any]] = field(default_factory=list)
    #: the expert-tier value ids recorded in this conversation, so a claim that leans on one is labelled (B19)
    expert_ids: set[str] = field(default_factory=set)
    opened: bool = False

    def staging(self) -> Path:
        """The stage directory, made on first use: an action recorded before the first question (the MCP
        server's way in, a test) has somewhere to write without staging the opening evidence."""
        if self.stage is None:
            self.stage = Path(tempfile.mkdtemp(prefix="ue_chat_"))
        return self.stage

    def open(self) -> "Conversation":
        """Stage the standing material and the opening evidence, once per conversation."""
        if self.opened:
            return self
        self.opened = True
        stage = self.staging()
        (stage / "handbook.md").write_text(HANDBOOK.read_text())
        (stage / "criteria.toml").write_text(CRITERIA_FILE.read_text())
        for tool in ("cell_scores", "cell_features", "criteria_breakdown", "label_context"):
            self.record(T.call(tool, {"cell_id": self.cell_id}))
        return self

    def record(self, result: T.ToolResult) -> T.ToolResult:
        """A tool result joins the registry, the quotable context and the stage, and is listed as a call."""
        stage = self.staging()
        self.values |= result.values
        payload = json.dumps(result.as_json(), indent=1)
        self.context += "\n" + quotable(result.as_json())
        name = f"tool_{len(self.calls) + 1:02d}_{result.tool}.json"
        (stage / name).write_text(payload)
        self.calls.append({"tool": result.tool, "args": result.args, "file": name,
                           "rows": len(result.rows), "values": len(result.values)})
        return result

    def record_action(self, tool: str, payload: dict[str, Any], expert: bool = False,
                      quoted: bool = True) -> str:
        """An action's result (an abstention, an insight) staged and listed the way a tool result is, so the
        persisted turn names it. With `quoted` the payload's strings join the context the gate reads, as a
        tool result's do (an insight's words are the geologist's, quotable back); an abstention's are the
        model's own and never a source of a number, so it is staged without joining. Expert-tier values are
        remembered by id (B19)."""
        stage = self.staging()
        values = dict(payload.get("values") or {})
        self.values |= values
        if expert:
            self.expert_ids |= set(values)
        if quoted:
            self.context += "\n" + quotable(payload)
        name = f"tool_{len(self.calls) + 1:02d}_{tool}.json"
        (stage / name).write_text(json.dumps(payload, indent=1, default=str))
        self.calls.append({"tool": tool, "args": {k: v for k, v in payload.items() if k in ("reason", "author", "job_id")},
                           "file": name, "rows": 0, "values": len(values)})
        return name

    def staged(self, tool: str, args: dict[str, Any]) -> dict[str, Any] | None:
        """The call already staged with exactly these arguments, if any: a plan reuses it rather than asking
        the store the same question twice in one conversation."""
        return next((c for c in self.calls if c["tool"] == tool and c["args"] == args), None)

    def transcript(self) -> str:
        lines = []
        for t in self.turns[-6:]:
            lines.append(f"  user: {t['question']}")
            lines.append(f"  you:  {t.get('text') or '(not shown: failed the evidence check)'}")
        return "\n".join(lines) or "  (this is the first question)"


__all__ = ["Conversation", "LOCAL"]
