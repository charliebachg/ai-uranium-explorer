"""The Analyst v1 wire protocol: what a plan, a node, a verifier verdict, a decision and a chain look like on
disk and on the wire between the stages of the staged loop (PRD 8.4).

The loop is STA-CoT's (planner, executor per segment, mechanical rule check, model chain verifier with
node-level feedback, K refinement rounds, majority over rounds) adapted to structured evidence, and the
protocol is where the adaptation lives:

* **A node is a claim about one criterion, not a sentence.** Its status has three values because unknown is
  not absent: a feature nobody measured here is `unknown`, a feature measured and not met is `not_met`. Its
  `strength` is an integer the weighted-sum decider can use and the mechanical gate can check; it must be 0
  when the status is unknown, because a strength on an unmeasured feature is a number from nowhere.
* **Every number cites a value id.** `value_ids` is the list `memo.check_claims` binds the text's numbers to;
  `expert_ids` names the expert-tier ids among them (B19), so a reader can see when a chain leans on a
  reading no automated tier could check.
* **`depends_on` is the chain.** A criterion node depends on nothing; a cross-check node names the criterion
  nodes it combines. When the verifier faults a node, the harness re-executes it and everything that depends
  on it, and this list is how it knows what that is.
* **The schemas cover only what a model returns.** `NODE_SCHEMA` (and `NODES_SCHEMA`, a list of them for
  the batch executor arm), `VERIFIER_SCHEMA`, `PLAN_SCHEMA` and `ADJUDICATOR_SCHEMA` are handed to the
  backend; the harness fills the rest (ids, round, attempt, model, cost) so a model can never claim to have
  been published.

Validation is explicit: `validate()` raises `ValueError` naming the field, `check()` lists every structural
problem, and `from_dict` validates on the way in. A chain stores only nodes that were published or finally
recorded, so what is on disk always validates; the gate (`nodegate.check_node`) sees the model's raw node
through `check()` and reports the same problems as feedback instead of raising.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field, fields
from typing import Any, Iterable

from ..prospect.tools import TOOL_HELP
from .v0 import ABSTAIN, ANSWER_SCHEMA, VERDICTS

SCHEMA_VERSION = "1.0.0"

SEGMENT_KINDS = ("criterion", "crosscheck", "retrieval")
NODE_STATUSES = ("met", "not_met", "unknown")
PLANNERS = ("template", "model")
PURPOSES = ("dashboard", "scored", "benchmark")
#: the cross-checks the handbook asks for: pathway ∧ trap, and geochemistry conditioned on how much sampling
#: stands behind it. A cross-check segment's `criterion` is one of these pair keys.
CROSSCHECKS = ("conductor_fault", "sediment_sampling")
#: what the executor may be asked to run: the panel's tools plus the two deterministic neighbourhood tools
ALLOWED_TOOLS = tuple(dict.fromkeys((*TOOL_HELP, "nearby", "crosscheck")))

#: the third failed attempt at a node is recorded as unknown, never retried (STA-CoT's rule controller)
MAX_ATTEMPTS = 3
STRENGTH_MAX = 5
TEXT_MAX = 300
FEEDBACK_MAX = 600
RATIONALE_MAX = 400

SEGMENT_ID = re.compile(r"^s\d{2,}$")
NODE_ID = re.compile(r"^n\d{2,}$")

ADJUDICATOR_SCHEMA: dict[str, Any] = ANSWER_SCHEMA

#: value-id kinds that name the grid, not a cell: coverage shares, passage pages and the model metrics
CELL_LESS_KINDS = ("cov", "pass", "metric")


# ---------------------------------------------------------------- value ids

def parse_id(vid: str) -> tuple[str | None, str, tuple[str, ...]]:
    """(cell, kind, suffix) of a value id in either scheme: the live `c:<kind>:<cell>:<suffix>` or the
    benchmark's anonymised `b:<bench>:<kind>:<suffix>`. Cell-less kinds (`c:cov:<feature>`) return None as
    the cell; anything that is not a value id returns an empty kind so the caller can name it."""
    parts = vid.split(":")
    if len(parts) >= 3 and parts[0] == "b":
        return parts[1], parts[2], tuple(parts[3:])
    if len(parts) >= 3 and parts[0] == "c":
        if parts[1] in CELL_LESS_KINDS:
            return None, parts[1], tuple(parts[2:])
        return parts[2], parts[1], tuple(parts[3:])
    return None, "", (vid,)


# ---------------------------------------------------------------- field checks

def _str(problems: list[str], name: str, value: Any, required: bool = True, max_len: int | None = None) -> None:
    if value is None or value == "":
        if required:
            problems.append(f"{name}: required")
        return
    if not isinstance(value, str):
        problems.append(f"{name}: must be a string, not {type(value).__name__}")
    elif max_len is not None and len(value) > max_len:
        problems.append(f"{name}: {len(value)} characters; the limit is {max_len}")


def _enum(problems: list[str], name: str, value: Any, allowed: Iterable[str], nullable: bool = False) -> None:
    if value is None and nullable:
        return
    if value not in tuple(allowed):
        problems.append(f"{name}: {value!r} is not one of {tuple(allowed)}")


def _str_list(problems: list[str], name: str, value: Any) -> None:
    if not isinstance(value, list) or not all(isinstance(v, str) for v in value):
        problems.append(f"{name}: must be a list of strings")


def _number(problems: list[str], name: str, value: Any, lo: float, hi: float, integer: bool = False) -> None:
    ok = isinstance(value, (int, float)) and not isinstance(value, bool) and value == value
    if integer and not isinstance(value, int):
        ok = False  # 4.0 is not an integer strength: JSON that meant an integer says one
    if not ok:
        problems.append(f"{name}: must be {'an integer' if integer else 'a number'}, not {value!r}")
    elif not lo <= float(value) <= hi:
        problems.append(f"{name}: {value!r} is outside {lo:g}..{hi:g}")


def _criterion_for_kind(problems: list[str], kind: str, criterion: Any) -> None:
    """The one rule about `criterion` that both a segment and a node obey: a criterion segment names a
    criteria-table key, a cross-check names its pair, a retrieval names nothing."""
    if kind == "criterion":
        _str(problems, "criterion", criterion)
    elif kind == "crosscheck":
        _enum(problems, "criterion", criterion, CROSSCHECKS)
    elif kind == "retrieval" and criterion is not None:
        problems.append(f"criterion: a retrieval segment has no criterion, not {criterion!r}")


def _raise_first(problems: list[str]) -> None:
    if problems:
        raise ValueError(problems[0])


def _only_fields(cls: type, d: dict[str, Any]) -> dict[str, Any]:
    """A record read back may carry keys a later version added; the dataclass takes its own."""
    names = {f.name for f in fields(cls)}
    unknown = set(d) - names
    if unknown - {"schema_version"}:
        raise ValueError(f"{cls.__name__}: unknown fields {sorted(unknown - {'schema_version'})}")
    return {k: v for k, v in d.items() if k in names}


# ---------------------------------------------------------------- the plan

@dataclass
class Segment:
    """One step of the plan. `tool_calls` are run by the harness before the executor is called (the executor
    never calls a tool live; it reads staged results), so their `args` may carry the `$cell` placeholder
    `plan.bind` substitutes. `depends_on` names the segments whose nodes this one may read: empty for a
    criterion, the criterion segments a cross-check combines."""

    segment_id: str
    kind: str
    criterion: str | None
    purpose: str
    tool_calls: list[dict[str, Any]] = field(default_factory=list)
    depends_on: list[str] = field(default_factory=list)

    def check(self) -> list[str]:
        p: list[str] = []
        if not isinstance(self.segment_id, str) or not SEGMENT_ID.match(self.segment_id):
            p.append(f"segment_id: {self.segment_id!r} is not of the form s01")
        _enum(p, "kind", self.kind, SEGMENT_KINDS)
        _criterion_for_kind(p, self.kind, self.criterion)
        _str(p, "purpose", self.purpose)
        if not isinstance(self.tool_calls, list):
            p.append("tool_calls: must be a list")
        else:
            for i, call in enumerate(self.tool_calls):
                if not isinstance(call, dict) or not isinstance(call.get("tool"), str):
                    p.append(f"tool_calls[{i}]: must be {{tool, args}}")
                    continue
                _enum(p, f"tool_calls[{i}].tool", call["tool"], ALLOWED_TOOLS)
                if not isinstance(call.get("args", {}), dict):
                    p.append(f"tool_calls[{i}].args: must be an object")
        _str_list(p, "depends_on", self.depends_on)
        if self.kind == "criterion" and self.depends_on:
            p.append("depends_on: a criterion segment depends on nothing")
        return p

    def validate(self) -> "Segment":
        _raise_first(self.check())
        return self

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Segment":
        return cls(**_only_fields(cls, d)).validate()


@dataclass
class Plan:
    planner: str
    segments: list[Segment]
    prompt_version: str

    def check(self) -> list[str]:
        p: list[str] = []
        _enum(p, "planner", self.planner, PLANNERS)
        _str(p, "prompt_version", self.prompt_version)
        seen: list[str] = []
        for s in self.segments:
            p += [f"segments[{s.segment_id}].{m}" for m in s.check()]
            if s.segment_id in seen:
                p.append(f"segments: segment_id {s.segment_id!r} appears twice")
            for dep in s.depends_on:
                if dep not in seen:  # earlier segments only: a plan is executed in order
                    p.append(f"segments[{s.segment_id}].depends_on: {dep!r} is not an earlier segment")
            seen.append(s.segment_id)
        return p

    def validate(self) -> "Plan":
        _raise_first(self.check())
        return self

    def segment(self, segment_id: str) -> Segment:
        for s in self.segments:
            if s.segment_id == segment_id:
                return s
        raise KeyError(segment_id)

    def as_dict(self) -> dict[str, Any]:
        return {"planner": self.planner, "prompt_version": self.prompt_version,
                "segments": [s.as_dict() for s in self.segments]}

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Plan":
        d = _only_fields(cls, d)
        segments = [Segment.from_dict(s) if isinstance(s, dict) else s for s in d.get("segments") or []]
        return cls(planner=d.get("planner", ""), segments=segments,
                   prompt_version=d.get("prompt_version", "")).validate()


PLAN_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["segments"],
    "properties": {
        "segments": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["segment_id", "kind", "criterion", "purpose", "tool_calls", "depends_on"],
                "properties": {
                    "segment_id": {"type": "string", "pattern": SEGMENT_ID.pattern},
                    "kind": {"type": "string", "enum": list(SEGMENT_KINDS)},
                    "criterion": {"type": ["string", "null"]},
                    "purpose": {"type": "string"},
                    "tool_calls": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "additionalProperties": False,
                            "required": ["tool", "args"],
                            "properties": {
                                "tool": {"type": "string", "enum": list(ALLOWED_TOOLS)},
                                "args": {"type": "object", "additionalProperties": True},
                            },
                        },
                    },
                    "depends_on": {"type": "array", "items": {"type": "string"}},
                },
            },
        },
    },
}


# ---------------------------------------------------------------- the node

@dataclass
class Node:
    """One executed segment. The model returns the `NODE_SCHEMA` part; the harness sets the identity
    (`node_id`, `segment_id`, `kind`, `depends_on`), the bookkeeping (`round`, `attempt`, `model`, cost)
    and the outcome (`published`, `problems`). Round 0 is the first construction; a re-execution after the
    verifier faults a node is round 1 and up. `attempt` counts gate rejections within a round."""

    node_id: str
    segment_id: str
    kind: str
    criterion: str | None
    status: str
    strength: int
    value_ids: list[str] = field(default_factory=list)
    text: str = ""
    depends_on: list[str] = field(default_factory=list)
    expert_ids: list[str] = field(default_factory=list)
    unknown_reason: str | None = None
    round: int = 0
    attempt: int = 1
    model: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0
    published: bool = False
    problems: list[str] = field(default_factory=list)

    def check(self) -> list[str]:
        p: list[str] = []
        if not isinstance(self.node_id, str) or not NODE_ID.match(self.node_id):
            p.append(f"node_id: {self.node_id!r} is not of the form n01")
        if not isinstance(self.segment_id, str) or not SEGMENT_ID.match(self.segment_id):
            p.append(f"segment_id: {self.segment_id!r} is not of the form s01")
        _enum(p, "kind", self.kind, SEGMENT_KINDS)
        _criterion_for_kind(p, self.kind, self.criterion)
        _enum(p, "status", self.status, NODE_STATUSES)
        _number(p, "strength", self.strength, 0, STRENGTH_MAX, integer=True)
        if self.status == "unknown" and self.strength != 0:
            p.append(f"strength: must be 0 when the status is unknown, not {self.strength!r}")
        _str_list(p, "value_ids", self.value_ids)
        _str_list(p, "expert_ids", self.expert_ids)
        if isinstance(self.expert_ids, list) and isinstance(self.value_ids, list):
            stray = [e for e in self.expert_ids if e not in self.value_ids]
            if stray:
                p.append(f"expert_ids: {stray} are not in value_ids")
        _str(p, "text", self.text, max_len=TEXT_MAX)
        _str_list(p, "depends_on", self.depends_on)
        if self.kind == "criterion" and self.depends_on:
            p.append("depends_on: a criterion node depends on nothing")
        _str(p, "unknown_reason", self.unknown_reason, required=False)
        _number(p, "round", self.round, 0, 10**6, integer=True)
        _number(p, "attempt", self.attempt, 1, MAX_ATTEMPTS, integer=True)
        _str(p, "model", self.model, required=False)
        _number(p, "cost_usd", self.cost_usd, 0, 10**6)
        _number(p, "duration_s", self.duration_s, 0, 10**9)
        if not isinstance(self.published, bool):
            p.append("published: must be true or false")
        _str_list(p, "problems", self.problems)
        return p

    def validate(self) -> "Node":
        _raise_first(self.check())
        return self

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Node":
        return cls(**_only_fields(cls, d)).validate()

    @classmethod
    def from_model(cls, payload: dict[str, Any], node_id: str, segment: Segment, depends_on: Iterable[str] = (),
                   round: int = 0, attempt: int = 1) -> "Node":
        """The model's answer under the segment's identity, unvalidated so the gate can list its problems.
        A model that answers about a different criterion did not execute this segment, and that is refused
        here like a schema failure rather than argued with through feedback."""
        named = payload.get("criterion")
        if named is not None and segment.criterion is not None and named != segment.criterion:
            raise ValueError(f"criterion: the model answered about {named!r}; segment {segment.segment_id} "
                             f"is about {segment.criterion!r}")
        return cls(
            node_id=node_id, segment_id=segment.segment_id, kind=segment.kind, criterion=segment.criterion,
            status=payload.get("status", ""), strength=payload.get("strength", -1),
            value_ids=list(payload.get("value_ids") or []), text=str(payload.get("text") or ""),
            depends_on=list(depends_on), expert_ids=list(payload.get("expert_ids") or []),
            unknown_reason=payload.get("unknown_reason"), round=round, attempt=attempt,
        )

    def line(self) -> str:
        """The node as one line of the chain: numbers appear only in `text`, beside the ids that back them."""
        ids = ", ".join(self.value_ids) or "-"
        deps = ", ".join(self.depends_on) or "-"
        what = f"{self.kind} {self.criterion}" if self.criterion else self.kind
        text = " ".join(self.text.split())
        reason = f" (unknown_reason: {self.unknown_reason})" if self.status == "unknown" and self.unknown_reason else ""
        return (f"{self.node_id} | {self.segment_id} {what} | {self.status} | strength {self.strength} | "
                f"ids: {ids} | depends_on: {deps} | {text}{reason}")


NODE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["criterion", "status", "strength", "value_ids", "text"],
    "properties": {
        "criterion": {"type": ["string", "null"]},
        "status": {"type": "string", "enum": list(NODE_STATUSES)},
        "strength": {"type": "integer", "minimum": 0, "maximum": STRENGTH_MAX},
        "value_ids": {"type": "array", "items": {"type": "string"}},
        "text": {"type": "string", "maxLength": TEXT_MAX},
        "expert_ids": {"type": "array", "items": {"type": "string"}},
        "unknown_reason": {"type": "string"},
    },
}

#: the reply of the batch executor (`executor_batch`): one node per criterion it was asked for, in one call.
#: The harness splits the list by `criterion` and gates every node as it gates a per-segment answer.
NODES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["nodes"],
    "properties": {"nodes": {"type": "array", "items": NODE_SCHEMA}},
}


# ---------------------------------------------------------------- the verifier

@dataclass
class VerifierVerdict:
    """One chain-verify round. `candidate_label` and `candidate_probability` are the verifier's own answer,
    recorded so agreement with the deciders is a per-stage metric; nothing acts on them."""

    round: int
    valid: bool
    faulty: list[dict[str, str]] = field(default_factory=list)
    feedback: str = ""
    candidate_label: str = ABSTAIN
    candidate_probability: float = 0.5
    rationale: str = ""
    model: str = ""
    cost_usd: float = 0.0
    duration_s: float = 0.0

    def check(self) -> list[str]:
        p: list[str] = []
        _number(p, "round", self.round, 0, 10**6, integer=True)
        if not isinstance(self.valid, bool):
            p.append("valid: must be true or false")
        if not isinstance(self.faulty, list):
            p.append("faulty: must be a list of {node_id, reason}")
        else:
            for i, f in enumerate(self.faulty):
                if not isinstance(f, dict) or not isinstance(f.get("node_id"), str) \
                        or not isinstance(f.get("reason"), str):
                    p.append(f"faulty[{i}]: must be {{node_id, reason}}")
                elif not NODE_ID.match(f["node_id"]):
                    p.append(f"faulty[{i}].node_id: {f['node_id']!r} is not of the form n01")
        if self.valid is False and not self.faulty and not self.feedback:
            p.append("feedback: an invalid chain needs faulty nodes or chain-level feedback")
        _str(p, "feedback", self.feedback, required=False, max_len=FEEDBACK_MAX)
        _enum(p, "candidate_label", self.candidate_label, VERDICTS)
        _number(p, "candidate_probability", self.candidate_probability, 0, 1)
        _str(p, "rationale", self.rationale, required=False, max_len=RATIONALE_MAX)
        _str(p, "model", self.model, required=False)
        _number(p, "cost_usd", self.cost_usd, 0, 10**6)
        _number(p, "duration_s", self.duration_s, 0, 10**9)
        return p

    def validate(self) -> "VerifierVerdict":
        _raise_first(self.check())
        return self

    def faulty_ids(self) -> list[str]:
        return [f["node_id"] for f in self.faulty]

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "VerifierVerdict":
        return cls(**_only_fields(cls, d)).validate()

    @classmethod
    def from_model(cls, payload: dict[str, Any], round: int) -> "VerifierVerdict":
        """The verdict as the model returned it, with its prose clipped to the protocol's lengths. The
        feedback and the rationale are guidance, not evidence, so cutting them loses nothing a gate checks;
        a provider that ignores a schema's maxLength once voided a whole chain for 72 characters of it."""
        return cls(round=round, valid=payload.get("valid", False), faulty=list(payload.get("faulty") or []),
                   feedback=_clip(str(payload.get("feedback") or ""), FEEDBACK_MAX),
                   candidate_label=payload.get("candidate_label", ""),
                   candidate_probability=payload.get("candidate_probability", -1.0),
                   rationale=_clip(str(payload.get("rationale") or ""), RATIONALE_MAX))


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


VERIFIER_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["valid", "faulty", "feedback", "candidate_label", "candidate_probability", "rationale"],
    "properties": {
        "valid": {"type": "boolean"},
        "faulty": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["node_id", "reason"],
                "properties": {"node_id": {"type": "string"}, "reason": {"type": "string"}},
            },
        },
        "feedback": {"type": "string", "maxLength": FEEDBACK_MAX},
        "candidate_label": {"type": "string", "enum": list(VERDICTS)},
        "candidate_probability": {"type": "number", "minimum": 0, "maximum": 1},
        "rationale": {"type": "string", "maxLength": RATIONALE_MAX},
    },
}


# ---------------------------------------------------------------- the decision

@dataclass
class Decision:
    """Stage 5. Both deciders are recorded whenever they ran: the weighted sum (with the version of the
    out-of-fold weights it used) and the adjudicator's v0-shaped answer. `majority_label` is STA-CoT's
    fallback vote over the rounds' candidate labels, set only when K rounds were exhausted. A chain that
    never validated and has no majority abstains: `final_verdict` is `insufficient` and `abstained_reason`
    carries the verifier's last feedback, counted as an abstention with its denominator, never a failure."""

    final_verdict: str
    final_probability: float
    weighted_score: float | None = None
    weights_version: str | None = None
    adjudicator: dict[str, Any] | None = None
    majority_label: str | None = None
    abstained_reason: str | None = None

    def check(self) -> list[str]:
        p: list[str] = []
        _enum(p, "final_verdict", self.final_verdict, VERDICTS)
        _number(p, "final_probability", self.final_probability, 0, 1)
        if self.weighted_score is not None:
            _number(p, "weighted_score", self.weighted_score, 0, 1)
        _str(p, "weights_version", self.weights_version, required=False)
        if (self.weighted_score is None) != (self.weights_version is None):
            p.append("weights_version: a weighted score names the weights it used, and only then")
        if self.adjudicator is not None:
            if not isinstance(self.adjudicator, dict):
                p.append("adjudicator: must be the adjudicator's answer object")
            else:
                missing = [k for k in ADJUDICATOR_SCHEMA["required"] if k not in self.adjudicator]
                if missing:
                    p.append(f"adjudicator: missing {missing}")
                _enum(p, "adjudicator.verdict", self.adjudicator.get("verdict"), VERDICTS)
        _enum(p, "majority_label", self.majority_label, VERDICTS, nullable=True)
        _str(p, "abstained_reason", self.abstained_reason, required=False)
        if self.abstained_reason is not None and self.final_verdict != ABSTAIN:
            p.append(f"abstained_reason: an abstention publishes {ABSTAIN!r}, not {self.final_verdict!r}")
        return p

    def validate(self) -> "Decision":
        _raise_first(self.check())
        return self

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Decision":
        return cls(**_only_fields(cls, d)).validate()


# ---------------------------------------------------------------- the chain

@dataclass
class Chain:
    """Everything one run of the loop produced over one cell, in the order it happened. `nodes` holds every
    node that was published or finally recorded, across rounds and attempts; the current chain is the latest
    of them per segment. `published` is Stage 6's whole-chain gate, not a node's."""

    chain_id: str
    cell: str
    purpose: str
    plan: Plan
    nodes: list[Node] = field(default_factory=list)
    verdicts: list[VerifierVerdict] = field(default_factory=list)
    decision: Decision | None = None
    published: bool = False
    run_id: str = ""
    manifest_sha256: str | None = None

    def check(self) -> list[str]:
        p: list[str] = []
        _str(p, "chain_id", self.chain_id)
        _str(p, "cell", self.cell)
        _enum(p, "purpose", self.purpose, PURPOSES)
        p += [f"plan.{m}" for m in self.plan.check()]
        segments = {s.segment_id: s for s in self.plan.segments}
        seen: set[str] = set()
        for n in self.nodes:
            p += [f"nodes[{n.node_id}].{m}" for m in n.check()]
            if n.node_id in seen:
                p.append(f"nodes: node_id {n.node_id!r} appears twice")
            seen.add(n.node_id)
            seg = segments.get(n.segment_id)
            if seg is None:
                p.append(f"nodes[{n.node_id}].segment_id: {n.segment_id!r} is not in the plan")
            elif (n.kind, n.criterion) != (seg.kind, seg.criterion):
                p.append(f"nodes[{n.node_id}].criterion: {n.kind} {n.criterion!r} does not match segment "
                         f"{seg.segment_id} ({seg.kind} {seg.criterion!r})")
            for dep in n.depends_on:
                if dep not in seen:
                    p.append(f"nodes[{n.node_id}].depends_on: {dep!r} is not an earlier node")
            if not n.published and not (n.status == "unknown" and n.problems):
                p.append(f"nodes[{n.node_id}].published: a rejected attempt is not stored; only a published "
                         f"node or the gate's final unknown record")
        for i, v in enumerate(self.verdicts):
            p += [f"verdicts[{i}].{m}" for m in v.check()]
            for fid in v.faulty_ids():
                if fid not in seen:
                    p.append(f"verdicts[{i}].faulty: {fid!r} is not a node of this chain")
        if self.decision is not None:
            p += [f"decision.{m}" for m in self.decision.check()]
        if self.published and self.decision is None:
            p.append("published: a chain is published with its decision, never without")
        _str(p, "run_id", self.run_id, required=False)
        _str(p, "manifest_sha256", self.manifest_sha256, required=False)
        return p

    def validate(self) -> "Chain":
        _raise_first(self.check())
        return self

    def current_nodes(self) -> list[Node]:
        """The chain as it stands: per segment, the latest stored node by (round, attempt), in plan order.
        A stored node is either published or the gate's final unknown record, so "latest" is the node the
        verifier and the deciders must see; a segment that has not been executed yet is absent."""
        latest: dict[str, Node] = {}
        for n in self.nodes:
            cur = latest.get(n.segment_id)
            if cur is None or (n.round, n.attempt) >= (cur.round, cur.attempt):
                latest[n.segment_id] = n
        order = {s.segment_id: i for i, s in enumerate(self.plan.segments)}
        return [latest[s] for s in sorted(latest, key=lambda s: (order.get(s, len(order)), s))]

    def chain_text(self) -> str:
        """The current chain rendered for the verifier and the adjudicator: one line per node with its id,
        criterion, status, strength, the ids it cites, what it depends on and its text. Every number on a
        line sits beside the ids that back it; there is nothing else numeric to quote."""
        # the chain id carries the run id, and a run id in the prompt would make the verifier's and the
        # adjudicator's calls miss the cache on every replay; the verifier needs the cell, not the run
        lines = [f"chain for cell {self.cell} ({self.purpose})",
                 "node | segment | status | strength | ids | depends_on | text"]
        lines += [n.line() for n in self.current_nodes()]
        return "\n".join(lines)

    def rounds(self) -> int:
        return max((n.round for n in self.nodes), default=-1) + 1

    def as_dict(self) -> dict[str, Any]:
        return {
            "schema_version": SCHEMA_VERSION, "chain_id": self.chain_id, "cell": self.cell,
            "purpose": self.purpose, "plan": self.plan.as_dict(), "nodes": [n.as_dict() for n in self.nodes],
            "verdicts": [v.as_dict() for v in self.verdicts],
            "decision": self.decision.as_dict() if self.decision is not None else None,
            "published": self.published, "run_id": self.run_id, "manifest_sha256": self.manifest_sha256,
        }

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "Chain":
        d = _only_fields(cls, d)
        plan = d.get("plan")
        decision = d.get("decision")
        return cls(
            chain_id=d.get("chain_id", ""), cell=d.get("cell", ""), purpose=d.get("purpose", ""),
            plan=Plan.from_dict(plan) if isinstance(plan, dict) else plan,
            nodes=[Node.from_dict(n) if isinstance(n, dict) else n for n in d.get("nodes") or []],
            verdicts=[VerifierVerdict.from_dict(v) if isinstance(v, dict) else v
                      for v in d.get("verdicts") or []],
            decision=Decision.from_dict(decision) if isinstance(decision, dict) else decision,
            published=bool(d.get("published", False)), run_id=str(d.get("run_id") or ""),
            manifest_sha256=d.get("manifest_sha256"),
        ).validate()
