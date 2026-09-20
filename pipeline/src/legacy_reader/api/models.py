"""Response models: the shapes the service promises, from which the web's client types are generated.

A value carries its id, so a number on screen can be walked back to the store; a tool result carries the values
it returned; a turn carries its claims with their value ids and the gate's objections. `extra="allow"` on the
value and the turn keeps new fields flowing to the client without a schema change, while the fields named here
are the ones the client may rely on."""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class Val(BaseModel):
    model_config = ConfigDict(extra="allow")
    id: str
    kind: str | None = None
    value: float | int | str | bool | None = None
    as_printed: str | None = None
    unit_as_printed: str | None = None
    unit: str | None = None
    fmt: str | None = None
    note: str | None = None


class ToolResultOut(BaseModel):
    tool: str
    args: dict[str, Any]
    note: str = ""
    rows: list[dict[str, Any]]
    values: dict[str, Val]


class Candidate(BaseModel):
    model_config = ConfigDict(extra="allow")
    cell_id: str
    score: float | None = None
    known_share: float | None = None
    lon: float
    lat: float
    in_basin: bool | None = None
    label_tier: str | None = None
    label_name: str | None = None
    km_to_label: float | None = None


class Cells(BaseModel):
    cells: list[Candidate]


class Claim(BaseModel):
    model_config = ConfigDict(extra="allow")
    claim_no: int | None = None
    text: str
    value_ids: list[str] = Field(default_factory=list)


class Memo(BaseModel):
    memo_id: str
    role: str
    verdict: str | None = None
    published: bool
    created_at: str
    claims: list[Claim]


class Evidence(BaseModel):
    cell_id: str
    lon: float | None = None
    lat: float | None = None
    in_basin: bool | None = None
    parts: dict[str, ToolResultOut]
    values: dict[str, Val]
    memos: list[Memo]
    #: the analyst chains stored for the cell (PRD §8.4 Stage 6), as `prospect.serve.evidence` shapes them; the
    #: web contract (`CellEvidence.chains`) is the typed side, so the route passes the dicts through untouched
    chains: list[dict[str, Any]] = Field(default_factory=list)


class Turn(BaseModel):
    model_config = ConfigDict(extra="allow")
    question: str
    text: str | None = None
    claims: list[Claim] = Field(default_factory=list)
    caveats: list[str] = Field(default_factory=list)
    cannot_answer: bool = False
    published: bool
    problems: list[str] = Field(default_factory=list)
    tools_used: list[str] = Field(default_factory=list)
    cost_usd: float | None = None
    asked_at: str | None = None
    usage_limited: bool = False


class ChatResponse(BaseModel):
    conversation_id: str
    cell_id: str
    turn: Turn
    values: dict[str, Val]
    cost_usd: float


class ToolCall(BaseModel):
    model_config = ConfigDict(extra="allow")
    tool: str
    args: dict[str, Any]
    file: str | None = None
    rows: int | None = None
    values: int | None = None


class StoredTurn(BaseModel):
    step: int
    question: str
    text: str | None = None
    published: bool
    problems: list[str]
    claims: list[Claim]
    tool_calls: list[ToolCall]
    values: dict[str, Val]
    cost_usd: float | None = None
    duration_s: float | None = None
    created_at: str


class ConversationRecord(BaseModel):
    conversation_id: str
    cell_id: str
    model: str
    backend: str
    created_at: str
    turns: list[StoredTurn]


class ConversationSummary(BaseModel):
    conversation_id: str
    model: str
    backend: str
    created_at: str
    turns: int
    cost_usd: float


class CellConversations(BaseModel):
    cell_id: str
    conversations: list[ConversationSummary]
