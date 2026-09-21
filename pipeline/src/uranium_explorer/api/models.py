"""Response models: the shapes the service promises, from which the web's client types are generated.

A value carries its id, so a number on screen can be walked back to the store; a tool result carries the values
it returned; a turn carries its claims with their value ids and the gate's objections. `extra="allow"` on the
value and the turn keeps new fields flowing to the client without a schema change, while the fields named here
are the ones the client may rely on."""

from __future__ import annotations

from typing import Any, Literal

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
    #: the analyst chains stored for the cell (the loop's publish step), as `prospect.serve.evidence` shapes them; the
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
    #: who asked: the key's label (never the key), `local` with no register; null on rows older than the roles
    requested_by: str | None = None


class ConversationRecord(BaseModel):
    conversation_id: str
    cell_id: str
    model: str
    backend: str
    created_at: str
    requested_by: str | None = None
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


# ---------------------------------------------------------------- who is calling, and the jobs


class Whoami(BaseModel):
    """The principal a key resolves to: a label that is not the key, its scopes, and the roles they cover."""

    name: str
    scopes: list[str]
    roles: list[str]


JobStatus = Literal["queued", "running", "done", "failed", "cancelled"]


class JobRequest(BaseModel):
    kind: str = Field(min_length=1, max_length=40, description="a registered job kind: analyst")
    cell_id: str | None = Field(default=None, pattern=r"^\d{4}_\d{4}$", description="looks like 0123_0045")
    args: dict[str, Any] = Field(default_factory=dict,
                                 description="the kind's arguments; for analyst: budget_usd, arm, reason, expert_ids")


class Job(BaseModel):
    """One background job's row, as `agent.job` holds it: the status is the whole story, the progress list says
    which stages have run, and the result names what the job produced (for analyst: the chain id, the verdict
    and the cost). A job that was running when the process restarted is failed with that reason."""

    job_id: str
    kind: str
    cell_id: str | None = None
    status: JobStatus
    requested_by: str
    args: dict[str, Any] = Field(default_factory=dict)
    created_at: str
    started_at: str | None = None
    finished_at: str | None = None
    progress: list[dict[str, Any]] = Field(default_factory=list)
    result: dict[str, Any] | None = None
    error: str | None = None
    run_id: str | None = None


class CellJobs(BaseModel):
    cell_id: str
    jobs: list[Job]
