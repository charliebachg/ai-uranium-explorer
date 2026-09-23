"""The intent router: one cheap structured call that says what kind of question this is.

The router classifies and names; it never answers. Its output is a kind from a fixed set, the cells and the
things the question names, and whether the question is outside what the record can say at all. Each kind
then has a plan written here in Python: which tools to call with which arguments. The model decides what is
being asked; the plan decides what is read; the answer call (in `agent`) says what was found, gated. A reply
the router cannot be read as (a model that ignored the schema, a kind nothing here knows) is routed as
`other`, which is the plain tool loop, so a router failure costs a few tool calls and never an answer.
"""

from __future__ import annotations

import re
from dataclasses import asdict, dataclass, field
from typing import Any

from ..backends.base import UsageLimitReached
from ..bench.pack import CELL_ID
from ..prospect import tools as T
from . import model as M
from .conversation import Conversation
from .readers_tool import TOOL as READERS
from .sensitivity import TOOL as SENSITIVITY

PROMPT_VERSION = "interface/router/v1"
#: the fixed kinds, in the order the prompt lists them
KINDS: tuple[str, ...] = ("lookup", "compare", "explain_score", "what_is_unknown", "what_would_change",
                          "record_insight", "run_analyst", "other")
#: what a lookup may be about, and the tool that holds it
TOPICS: dict[str, str] = {
    "features": "cell_features", "scores": "cell_scores", "criteria": "criteria_breakdown",
    "labels": "label_context", "coverage": "coverage", "passages": "retrieve", "nearby": "nearby",
    "crosscheck": "crosscheck", "sensitivity": SENSITIVITY, "readers": READERS,
}
#: one line per kind, for the router's prompt and the panel's route line
MEANING: dict[str, str] = {
    "lookup": "a value the store holds for this cell: a feature, a score, a criterion's state, the nearest "
              "deposit, a layer's coverage, what the reports say",
    "compare": "the same values on two cells side by side; the question names a second cell id",
    "explain_score": "why the scores are what they are: what carried the criteria score, and how much of the "
                     "learned score is exploration history",
    "what_is_unknown": "which criteria are unknown here rather than not met, and how thin the features behind "
                       "them are",
    "what_would_change": "which single unmeasured criterion would move the reading most if it were measured",
    "record_insight": "the person states something of their own about this ground to be recorded, not asked",
    "run_analyst": "the person asks for the analyst to run (or re-run) on this cell",
    "other": "anything else the tools can be asked about; a plain tool loop answers it",
}

ROUTER_SYSTEM = f"""You classify one question about one 2 km cell of public Saskatchewan exploration data.
You do not answer it. You never state a number. You pick exactly one kind, name the cells and the things the
question is about, and say whether it is out of scope.

The kinds:
{chr(10).join(f"  {k}: {MEANING[k]}" for k in KINDS)}

Topics a lookup or a compare may be about (pick the one that holds the value):
  features    a measured feature of the cell (distance to a conductor or fault, sediment uranium, holes drilled)
  scores      the criteria, learned or effort score, or how much of a score's inputs are known
  criteria    a targeting criterion's state here (met, not met, unknown), its weight or threshold
  labels      the nearest known deposit or occurrence and its distance
  coverage    how much of the grid a feature covers
  passages    what the assessment reports say about this ground
  nearby      what one evidence layer holds around the cell (a count, the nearest feature)
  crosscheck  a conductor beside a fault, a sediment anomaly beside its sampling
  sensitivity which unmeasured criterion would move the score most
  readers     what the evidence readers, the best analyst design, concluded here: their verdict and
              probability, and what each of the four readers found

Out of scope, whatever the wording: a company's holdings or claims, a grade or tonnage, an ore body, where to
drill or whether to drill, a recommendation to buy or stake, anything about ground outside this grid, and
anything that is not about this record at all. Mark those out_of_scope with a short detail; keep the kind as
your best reading of the question.

entities: the feature keys, criterion keys, layer names, model names or words the question names, verbatim.
cell_ids: every cell id written like 0123_0045 in the question, in order. For record_insight, insight_text
is the person's statement itself, verbatim, without the words that asked for it to be recorded."""

ROUTE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "required": ["kind"],
    "properties": {
        "kind": {"type": "string", "enum": list(KINDS)},
        "topic": {"type": "string", "enum": list(TOPICS)},
        "cell_ids": {"type": "array", "items": {"type": "string"}},
        "entities": {"type": "array", "items": {"type": "string"}},
        "insight_text": {"type": "string"},
        "out_of_scope": {"type": "boolean"},
        "detail": {"type": "string"},
        "reason": {"type": "string"},
    },
}


@dataclass
class Route:
    """What the router decided, as the panel and the plan read it."""

    kind: str
    topic: str | None = None
    cell_ids: list[str] = field(default_factory=list)
    entities: list[str] = field(default_factory=list)
    insight_text: str = ""
    out_of_scope: bool = False
    detail: str = ""
    reason: str = ""
    #: the router's reply could not be read; the kind is `other` and the panel says so
    fallback: str | None = None

    def as_json(self) -> dict[str, Any]:
        return asdict(self)


def _prompt(conv: Conversation, question: str) -> str:
    return f"""Cell {conv.cell_id}.

Earlier in this conversation:
{conv.transcript()}

The question: {question}

Classify it."""


def parse(reply: dict[str, Any], question: str, cell_id: str) -> Route:
    """The router's reply as a `Route`, with the cell ids the question itself carries whatever the model
    listed, so a compare never rests on the model's transcription of an id."""
    kind = reply.get("kind")
    if kind not in KINDS:
        return Route(kind="other", fallback=f"the router replied with kind {kind!r}, which is not one of the kinds")
    topic = reply.get("topic")
    topic = topic if topic in TOPICS else None
    named = [m.group(0) for m in CELL_ID.finditer(question)]
    listed = [c for c in (reply.get("cell_ids") or []) if isinstance(c, str) and CELL_ID.fullmatch(c)]
    cells = list(dict.fromkeys([cell_id, *named, *listed]))
    return Route(
        kind=str(kind), topic=topic, cell_ids=cells,
        entities=[str(e) for e in (reply.get("entities") or []) if isinstance(e, str) and e.strip()],
        insight_text=str(reply.get("insight_text") or "").strip(),
        out_of_scope=bool(reply.get("out_of_scope")), detail=str(reply.get("detail") or "").strip(),
        reason=str(reply.get("reason") or "").strip(),
    )


def route(conv: Conversation, question: str, backend: Any, model: str, effort: str, on_event: M.Event
          ) -> tuple[Route, float]:
    """One structured call, and what it cost. A backend that raises a usage limit routes as `other` with the
    limit named, so the turn can still say what happened."""
    req = M.request(task="interface_route", stage=None, system=ROUTER_SYSTEM, prompt=_prompt(conv, question),
                    schema=ROUTE_SCHEMA, prompt_version=PROMPT_VERSION, model=model, effort=effort)
    try:
        reply, cost = M.call(backend, req, on_event, 0)
    except UsageLimitReached as limit:
        return Route(kind="other", fallback=f"usage limit: {limit}"), 0.0
    return parse(reply, question, conv.cell_id), cost


# ---------------------------------------------------------------- the plans


@dataclass(frozen=True)
class Step:
    tool: str
    args: dict[str, Any]

    def as_json(self) -> dict[str, Any]:
        return {"tool": self.tool, "args": dict(self.args)}


_FEATURE_KEY = re.compile(r"^[a-z][a-z0-9_]+$")


def _layer(entities: list[str]) -> str | None:
    """The first entity that names an evidence layer `nearby` may open, if any."""
    return next((e for e in entities if e in T.NEARBY_LAYERS), None)


def _feature(entities: list[str]) -> str | None:
    """The first entity shaped like a feature key: `coverage` takes it, and refuses one it does not know."""
    return next((e for e in entities if _FEATURE_KEY.match(e)), None)


def _lookup(cell: str, route: Route, question: str) -> list[Step]:
    topic = route.topic or "features"
    tool = TOPICS[topic]
    if tool == "retrieve":
        return [Step("retrieve", {"query": question, "cell_id": cell, "k": 6})]
    if tool == "coverage":
        key = _feature(route.entities)
        return [Step("coverage", {"feature_key": key} if key else {"feature_key": None})]
    if tool == "nearby":
        layer = _layer(route.entities)
        # a layer the question did not name: the crosscheck reads the two the handbook pairs, deterministically
        return ([Step("nearby", {"cell_id": cell, "layer": layer, "radius_m": 5000.0, "k": 5})] if layer
                else [Step("crosscheck", {"cell_id": cell})])
    # the four opening reads are staged with the cell id alone; the same arguments here mean they are reused
    return [Step(tool, {"cell_id": cell})]


def plan(route: Route, question: str) -> list[Step]:
    """The tool calls a routed kind makes, in order. Deterministic: the same route and question give the same
    steps, and nothing in a step came from the model but the cell ids it read off the question and the
    topic it chose. The kinds with an action instead of a read (`record_insight`, `run_analyst`) and the
    unrouted kind (`other`) have no plan here; `agent` handles those."""
    cell = route.cell_ids[0]
    kind = route.kind
    if kind == "lookup":
        return _lookup(cell, route, question)
    if kind == "compare":
        others = route.cell_ids[1:2]
        if not others:
            return []
        topic = route.topic if route.topic in ("features", "criteria", "labels", "crosscheck") else None
        steps: list[Step] = []
        for c in (cell, *others):
            steps.append(Step("cell_scores", {"cell_id": c}))
            steps.append(Step(TOPICS[topic] if topic else "criteria_breakdown", {"cell_id": c}))
        return steps
    if kind == "explain_score":
        return [Step("cell_scores", {"cell_id": cell}), Step("criteria_breakdown", {"cell_id": cell})]
    if kind == "what_is_unknown":
        return [Step("criteria_breakdown", {"cell_id": cell}), Step("coverage", {"feature_key": None})]
    if kind == "what_would_change":
        return [Step(SENSITIVITY, {"cell_id": cell})]
    return []


def describe(route: Route, steps: list[Step]) -> dict[str, Any]:
    """The route line the stream carries and the turn keeps."""
    return {**route.as_json(), "meaning": MEANING.get(route.kind, ""), "plan": [s.as_json() for s in steps]}


__all__ = ["KINDS", "MEANING", "PROMPT_VERSION", "ROUTER_SYSTEM", "ROUTE_SCHEMA", "TOPICS", "Route", "Step",
           "describe", "parse", "plan", "route"]
