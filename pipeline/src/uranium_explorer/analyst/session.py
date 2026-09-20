"""The analyst session: the only path from a tool to a model, and where the leakage rules live.

Every stage of the staged analyst (PRD §8.4) asks for evidence through one of these. A stage never calls a
tool directly, because the four leakage controls the bias register names are properties of *which rows reach
the model*, and a rule about rows has to sit on the path the rows travel:

* **B17, retrieval leakage.** In a scored or benchmark session `retrieve` is always given the cell's
  blind-list as `exclude_files`, and a passage that still names a blind-listed file is dropped here and
  counted. Two layers, because the second is what makes a regression in the first visible.
* **B18, score leakage.** The served `cell_scores` table was fitted on every label, this cell's included. A
  scored or benchmark session never calls it: it serves the out-of-fold scores for the session's fold, and
  records which model versions and which fold the model saw, so the manifest can say so.
* **B30, the cell's own label.** `label_context` is asked with the evaluated cell masked, and a row at 0.0 km
  is dropped regardless. The dashboard keeps it, because the nearest known occurrence is the first thing a
  skeptic should ask about a real cell.
* **B19, expert anchoring.** A value carrying the `expert` tier is remembered by id and the row that cites it
  is marked, so every node that leans on a geologist's insight says so and the diff can be reported.

The switches of an arm are enforced the same way: a part that is off is refused, not hidden behind prompt
wording, because a model told to ignore a table still reads it. A benchmark session anonymises and scrubs each
result exactly as the frozen packs were built, before it is recorded or written, so the model-facing id
scheme is `b:<bench_id>:…` and nothing that places the ground survives; a scored session keeps the real cell
id and scrubs the names. Every refusal names its rule, so a harness counts refusals per rule, not per
exception, and every span carries argument names and never their values, because a real cell id in a
benchmark run's trace is a leak too.

A call may carry a `Scope` (the `segment_scoped` arm): the staged file then holds only the rows the scope
names and the values they cite, and the session keeps a `View` per scope key, the registry a node of that
segment is gated against. The scope is applied after the leakage rules and the scrub, so it can never loosen
them, and it drops rows, never refuses them: a segment not shown another criterion's row is the view working
as designed, counted apart under `scoped_rows_dropped`. The private unscrubbed copy is the whole result
whatever the scope, because a dashboard cites from it and the scope is the harness's business.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Literal, Mapping

import pandas as pd

from ..bench import pack as P
from ..bench.blind import blind_list as compute_blind_list
from ..bench.card import DRILLHOLE_LAYER
from ..ids import sha256_json
from ..prospect import memo
from ..prospect import tools as T
from ..runtime.tracing import set_attrs, span
from ..store import connect
from .arms import Switches
from .plan import Scope
from .v0 import PLACE_NAMES

#: deposits, camps and regions the handbook names: scrubbed from every blinded result along with the store's
#: own names, because the executor reads the raw tool files and a criterion's literature line names them
PLACES: frozenset[str] = frozenset(PLACE_NAMES)

Purpose = Literal["dashboard", "scored", "benchmark"]
PURPOSES: tuple[str, ...] = ("dashboard", "scored", "benchmark")
#: what a refusal can be charged to: three leakage rules, an arm's switch, or the purpose of the session
RULES: tuple[str, ...] = ("B17", "B18", "B30", "switch", "purpose")
#: what a plan writes where the real cell id goes; in a benchmark the model never sees the real id
CELL_PLACEHOLDER = "$cell"
#: the radius a blind-list is computed with when the caller has no frozen one (the benchmark builder's own)
BLIND_RADIUS_KM = 10.0
#: a digit-free stand-in for the cell id while a scored session scrubs, so the cell-id pattern leaves it alone
_KEEP = "thiscell"
#: where a blinded session keeps the unscrubbed tool results: read by the dashboard, never staged to a model
PRIVATE_DIR = "private"


class SessionRefusal(Exception):
    """A call the session would not make, charged to the rule that refused it (`rule`, one of RULES).

    The message never carries a cell id: it ends up in a span's error field."""

    def __init__(self, rule: str, reason: str):
        super().__init__(f"{rule}: {reason}")
        self.rule = rule


def _cited(row: dict[str, Any]) -> set[str]:
    """The ids a row points at: every `*_id` key, which is how the tools bind a row to its values."""
    return {v for k, v in row.items() if k.endswith("_id") and isinstance(v, str)}



def _closed_book(payload: Any) -> Any:
    """The payload without any row's `evidence` field: the literature line behind a criterion, which names
    the deposits its threshold was read from. The caveat stays; it is scrubbed like everything else."""
    if isinstance(payload, dict):
        return {k: _closed_book(v) for k, v in payload.items() if k != "evidence"}
    if isinstance(payload, list):
        return [_closed_book(v) for v in payload]
    return payload

def _swap(node: Any, old: str, new: str) -> Any:
    """`old` replaced by `new` in every string value; keys are left alone, as `scrub` leaves them."""
    if isinstance(node, dict):
        return {k: _swap(v, old, new) for k, v in node.items()}
    if isinstance(node, list):
        return [_swap(v, old, new) for v in node]
    if isinstance(node, str):
        return node.replace(old, new)
    return node


@dataclass
class View:
    """What was staged under one scope key: the registry and the context a node of that segment is gated
    against. A value in the session's registry and in no view was staged to a role that reads the whole
    (the harness's own calls, the adjudicator on the shallow path), never to that segment's executor."""

    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    context: str = ""


@dataclass
class Session:
    """One purpose's run over one cell: what it asked for, what it got, what it may cite, and what it refused."""

    cell_id: str
    purpose: str
    fold: int | None
    switches: Switches
    stage: Path
    #: the id the model sees: the bench id in a benchmark, the cell id otherwise
    bench_id: str
    #: file numbers retrieval may not return, sorted so the hash is stable; empty on the dashboard
    blind_list: list[str]
    tools: Mapping[str, Callable[..., T.ToolResult]] = field(repr=False)
    #: names no blinded result may carry: company, property and deposit names, hole names, and in a benchmark
    #: the cell id itself
    forbidden: frozenset[str] = frozenset()
    oof: pd.DataFrame | None = field(default=None, repr=False)
    con: Any = field(default=None, repr=False)
    owns_con: bool = False
    #: everything the model may cite, keyed in the id scheme the model sees
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    #: everything the tools returned as text, so the gate can tell a quoted name from an invented number
    context: str = ""
    calls: list[dict[str, Any]] = field(default_factory=list)
    #: the model version and fold of every score served, for the manifest
    scores_seen: list[dict[str, Any]] = field(default_factory=list)
    expert_ids: set[str] = field(default_factory=set)
    #: every refusal, a whole call or a single row, by the rule that refused it
    refusals: dict[str, int] = field(default_factory=dict)
    #: effort rows removed because the arm's switch is off: a switch acting as designed, not a refusal
    effort_rows_dropped: int = 0
    #: per scope key, what was staged under it: the registry a node of that segment may cite from
    views: dict[str, View] = field(default_factory=dict)
    #: table rows a scope left out of a staged file: the segment's view acting as designed, not a refusal
    scoped_rows_dropped: int = 0

    @classmethod
    def open(cls, cell_id: str, purpose: Purpose, *, fold: int | None, switches: Switches,
             bench_id: str | None = None, blind: list[str] | None = None, con: Any = None, stage: Path,
             tools: Mapping[str, Callable[..., T.ToolResult]] | None = None, oof: pd.DataFrame | None = None,
             forbidden: set[str] | None = None) -> "Session":
        """A session over `cell_id` for one purpose.

        Scored and benchmark sessions are blinded: the blind-list is `blind` (the benchmark's frozen one) or
        computed from the store, and the forbidden names are `forbidden` (the caller's complete set, hole
        names included, as the harness builds it once per run) or read from the store with the hole names
        added. `tools` is the registry to dispatch to, the live one by default. The store is opened read-only
        only when something has to be read from it, here or later for the out-of-fold scores, and closed by
        `close`: a session handed everything it needs never touches the store, which is what lets a test run
        beside a live chain run holding the file. The stage directory receives one file per tool result, as
        the memo panel's does."""
        if purpose not in PURPOSES:
            raise SessionRefusal("purpose", f"purpose must be one of {PURPOSES}, not {purpose!r}")
        if purpose == "benchmark" and not bench_id:
            raise SessionRefusal("purpose", "a benchmark session needs the bench id the model is shown")
        blinded = purpose != "dashboard"
        owns = blinded and con is None and (blind is None or forbidden is None)
        if owns:
            con = connect(read_only=True)
        files: list[str] = []
        names: set[str] = set()
        if blinded:
            files = sorted(str(f) for f in blind) if blind is not None else compute_blind_list(cell_id, BLIND_RADIUS_KM, con)
            names = set(forbidden) if forbidden is not None else P.forbidden_strings(con) | P.hole_names(con)
            if purpose == "benchmark":
                names.add(cell_id)
        stage.mkdir(parents=True, exist_ok=True)
        return cls(cell_id=cell_id, purpose=purpose, fold=fold, switches=switches, stage=stage,
                   bench_id=bench_id if purpose == "benchmark" else cell_id, blind_list=files,
                   tools=tools if tools is not None else T.REGISTRY, forbidden=frozenset(names), oof=oof,
                   con=con, owns_con=owns)

    def _store(self) -> Any:
        """The store connection, opened read-only on first need and owned by the session from then on."""
        if self.con is None:
            self.con = connect(read_only=True)
            self.owns_con = True
        return self.con

    @property
    def blinded(self) -> bool:
        return self.purpose != "dashboard"

    @property
    def blind_list_hash(self) -> str:
        return sha256_json(self.blind_list)

    def close(self) -> None:
        if self.owns_con and self.con is not None:
            self.con.close()
            self.con = None

    # ---------------------------------------------------------------- the guarded dispatcher

    def call(self, tool: str, args: dict[str, Any], scope: Scope | None = None) -> T.ToolResult:
        """One tool call, guarded: the switches, then the leakage rules, then the model-facing rewrite, then
        the scope when there is one, then the record. What comes back is exactly what was written to the
        stage and what the model may cite; under a scope, what that segment's model may cite."""
        fn = self.tools.get(tool)
        if fn is None:
            raise T.ToolError(f"no tool named {tool!r}; available: {', '.join(sorted(self.tools))}")
        with span(f"tool:{tool}", kind="tool", tool=tool, purpose=self.purpose, arg_keys=sorted(args)):
            try:
                real = self._real_args(args)
                self._enforce_switches(tool, real)
                result = self._serve(tool, fn, real)
            except SessionRefusal as refusal:
                self._refuse(refusal.rule)
                set_attrs(rule=refusal.rule)
                raise
            payload = self._model_facing(result, self._shown_args(args))
            if scope is not None:
                payload = self._scoped(scope, payload)
            self._mark_expert(payload)
            self._record(tool, payload, raw=result if self.blinded else None, scope=scope)
            set_attrs(n_values=len(payload["values"]), n_rows=len(payload["rows"]))
            if scope is not None:
                set_attrs(scope=scope.key)
        return T.ToolResult(tool, payload["args"], payload["rows"], payload["values"], payload["note"])

    def _real_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """The placeholder, or the id the model was shown, becomes the real cell id. A benchmark session refuses
        any other cell id outright: the model may not look ground up, its own or anyone else's."""
        out: dict[str, Any] = {}
        for k, v in args.items():
            if isinstance(v, str):
                if v in (CELL_PLACEHOLDER, self.bench_id):
                    v = self.cell_id
                elif self.purpose == "benchmark" and P.CELL_ID.search(v):
                    raise SessionRefusal("purpose", f"a benchmark session takes the cell as {CELL_PLACEHOLDER}, never by id")
            out[k] = v
        return out

    def _shown_args(self, args: dict[str, Any]) -> dict[str, Any]:
        """The arguments as the model sees them: the placeholder resolved to the id it knows the cell by."""
        return {k: (self.bench_id if v in (CELL_PLACEHOLDER, self.cell_id) else v) for k, v in args.items()}

    def _enforce_switches(self, tool: str, real: dict[str, Any]) -> None:
        sw = self.switches
        off = None
        if tool == "label_context" and not sw.label_context:
            off = "label_context"
        elif tool == "cell_scores" and not sw.oof_scores:
            off = "oof_scores"
        elif tool == "criteria_breakdown" and not sw.criteria:
            off = "criteria"
        elif tool == "nearby" and real.get("layer") == DRILLHOLE_LAYER and not sw.effort_features:
            off = "effort_features"
        if off:
            raise SessionRefusal("switch", f"{tool} is not available with {off} off in this arm")

    def _serve(self, tool: str, fn: Callable[..., T.ToolResult], real: dict[str, Any]) -> T.ToolResult:
        """The tool's result with the leakage rules applied, still in the real id scheme."""
        if tool == "cell_scores" and self.blinded:
            return self._oof_scores()
        kw = dict(real)
        if tool == "label_context" and self.blinded:
            kw["mask_cell"] = self.cell_id
        if tool == "retrieve" and self.blinded:
            kw["exclude_files"] = list(self.blind_list)
        try:
            result = fn(**kw)
        except TypeError as err:
            raise T.ToolError(f"{tool}: {err}") from err
        if tool == "cell_scores":
            self.scores_seen += [{"model_version": r.get("model"), "fold_kind": "served", "fold": None} for r in result.rows]
        if tool == "retrieve" and self.blinded:
            blind = set(self.blind_list)
            result = self._drop_rows(result, "B17", lambda r: str(r.get("file")) in blind)
        if tool == "label_context" and self.blinded:
            result = self._drop_rows(result, "B30", lambda r: r.get("distance_km") == 0.0)
        if not self.switches.effort_features:
            kept = P.drop_effort(result)
            self.effort_rows_dropped += len(result.rows) - len(kept.rows)
            result = kept
        return result

    def _oof_scores(self) -> T.ToolResult:
        """B18: the out-of-fold scores in place of the served table, and only the rows of this session's fold.
        A row from another fold came from a model that may have seen this cell's label."""
        if self.fold is None:
            raise SessionRefusal("B18", "a blinded session serves out-of-fold scores, which need the session's fold")
        served = P.oof_result(self.cell_id, con=self._store() if self.oof is None else None, oof=self.oof)
        result = self._drop_rows(served, "B18", lambda r: r.get("fold") != self.fold)
        self.scores_seen += [{"model_version": r["model"], "fold_kind": "spatial", "fold": r["fold"]} for r in result.rows]
        return result

    def _drop_rows(self, result: T.ToolResult, rule: str, doomed: Callable[[dict[str, Any]], bool]) -> T.ToolResult:
        """The result without the rows `doomed` names and without the values only those rows cite."""
        keep: list[dict[str, Any]] = []
        gone: set[str] = set()
        for row in result.rows:
            if doomed(row):
                gone |= _cited(row)
            else:
                keep.append(row)
        self._refuse(rule, len(result.rows) - len(keep))
        values = {k: v for k, v in result.values.items() if k not in gone}
        return T.ToolResult(result.tool, result.args, keep, values, result.note)

    def _refuse(self, rule: str, n: int = 1) -> None:
        if n:
            self.refusals[rule] = self.refusals.get(rule, 0) + n

    # ---------------------------------------------------------------- what the model sees

    def _model_facing(self, result: T.ToolResult, shown_args: dict[str, Any]) -> dict[str, Any]:
        """The payload as it is written and cited: anonymised then scrubbed in a benchmark, as `build_pack`
        does; scrubbed with the cell id kept in a scored run; untouched on the dashboard. The arguments are
        the model's own, so a keyword the session added (the mask, the blind-list) is never read back."""
        payload = result.as_json()
        del payload["args"]
        if self.blinded:
            # The frozen packs kept a criterion's literature `evidence` and `caveat` because their renderer
            # never printed them; the executor reads the raw file, so here the evidence line goes (it names
            # the deposits a threshold came from) and nothing is exempt from the scrub, the handbook's own
            # place names included.
            payload = _closed_book(payload)
            names = self.forbidden | PLACES
            if self.purpose == "benchmark":
                payload = P.scrub(P.anonymise(payload, self.cell_id, self.bench_id), names, keep=frozenset())
            else:
                # the cell-id pattern would otherwise cut the id out of every value it sits in
                payload = _swap(P.scrub(_swap(payload, self.cell_id, _KEEP), names, keep=frozenset()), _KEEP, self.cell_id)
        payload["args"] = shown_args
        return payload

    def _scoped(self, scope: Scope, payload: dict[str, Any]) -> dict[str, Any]:
        """The payload narrowed to the scope's rows and the values they cite, the note and the arguments
        kept. After the leakage rules and the scrub, so a scope never loosens them; before the record, so
        nothing the segment was not shown is citable under its key. The rows a scope leaves out are counted
        apart from the refusals: a segment not shown another criterion's row is the view as designed."""
        rows, values = scope.narrow(payload["tool"], payload["rows"], payload["values"])
        self.scoped_rows_dropped += len(payload["rows"]) - len(rows)
        return {**payload, "rows": rows, "values": values}

    def _mark_expert(self, payload: dict[str, Any]) -> None:
        """B19 on what is staged: every expert-tier value remembered by id and the row that cites it marked.
        After the scope, so an expert value in a row no segment saw is not counted as one a model leaned on."""
        expert = {vid for vid, v in payload["values"].items()
                  if isinstance(v, dict) and (v.get("tier") == "expert" or v.get("expert") is True)}
        for row in payload["rows"]:
            if _cited(row) & expert:
                row["expert"] = True
        self.expert_ids |= expert

    def _record(self, tool: str, payload: dict[str, Any], raw: T.ToolResult | None = None,
                scope: Scope | None = None) -> None:
        """The model-facing payload under `stage/`, and in a blinded session the unscrubbed result under
        `stage/private/`. Scrubbing drops file numbers and citations, which is right for what the model reads
        and wrong for what a geologist is later shown: the private copy is what a dashboard cites from, and it
        is never staged to a model because nothing stages the `private` directory. Under a scope the payload
        also joins that scope's view, and the call log names the scope; the staged file never does, because
        the scope is the harness's business and nothing a model reads should say a row was left out."""
        self.values |= payload["values"]
        self.context += "\n" + memo.quotable(payload)
        if scope is not None:
            view = self.views.setdefault(scope.key, View())
            view.values |= payload["values"]
            view.context += "\n" + memo.quotable(payload)
        n = len(self.calls) + 1
        path = self.stage / f"tool_{n:02d}_{tool}.json"
        path.write_text(json.dumps(payload, indent=1))
        entry = {"tool": tool, "args": payload["args"], "ids": sorted(payload["values"]),
                 "rows": len(payload["rows"]), "file": path.name}
        if raw is not None:
            private = self.stage / PRIVATE_DIR / path.name
            private.parent.mkdir(parents=True, exist_ok=True)
            private.write_text(json.dumps(raw.as_json(), indent=1))
            entry["private_file"] = f"{PRIVATE_DIR}/{path.name}"
        if scope is not None:
            entry["scope"] = scope.key
        self.calls.append(entry)

    # ---------------------------------------------------------------- the gate and the manifest

    def registry(self, scope: str | None = None) -> tuple[dict[str, dict[str, Any]], str]:
        """What a claim is gated against: the session's whole registry and context, or under a scope key
        the view staged under it. A key nothing was staged under sees nothing, which is the point: a node
        may cite only what its own segment was shown."""
        if scope is None:
            return self.values, self.context
        view = self.views.get(scope)
        return (view.values, view.context) if view is not None else ({}, "")

    def check_claims(self, claims: list[dict[str, Any]], scope: str | None = None) -> list[str]:
        """The memo gate over this session's registry and context, or over one scope's view: no looser path
        exists."""
        return memo.check_claims(claims, *self.registry(scope))

    def allowed_ids(self, scope: str | None = None) -> list[str]:
        """What a node may cite, sorted, for the gate's escalating feedback."""
        return sorted(self.registry(scope)[0])

    def manifest_fields(self) -> dict[str, Any]:
        """What the run manifest records about this session; every rule is present so manifests share a shape."""
        return {
            "purpose": self.purpose, "fold": self.fold, "blind_list_hash": self.blind_list_hash,
            "switches": asdict(self.switches), "scores_seen": list(self.scores_seen),
            "n_values": len(self.values), "n_expert_ids": len(self.expert_ids), "n_calls": len(self.calls),
            "refusals": {rule: self.refusals.get(rule, 0) for rule in RULES},
            "effort_rows_dropped": self.effort_rows_dropped,
            "scoped_rows_dropped": self.scoped_rows_dropped,
        }
