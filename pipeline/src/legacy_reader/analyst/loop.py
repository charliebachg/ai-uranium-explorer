"""Analyst v1: the staged analyst loop over one cell, PRD 8.4 Stages 0 to 6.

v0 asks one question and gates one answer. This loop asks one question per criterion, gates every answer
mechanically, has a second model read the whole chain for the flaw, repairs what it faults, and only then
rules: STA-CoT's planner / executor / verifier loop over the structured evidence a session serves. What this
module decides, and why:

* **The harness owns every identity and every count.** A model returns the `NODE_SCHEMA` part of a node and
  nothing else; node ids, rounds, attempts, dependencies, cost and the published flag are set here. Node ids
  are a running counter over the chain in plan order (`n01`.. for the first construction, continuing for
  re-executions), because the wire protocol stores every published or finally recorded node and needs each
  one addressable. The store reads the same nodes under a plan-position id with (round, attempt), which is
  what its `current_nodes` expects, so the two views of "the chain as it stands" agree.
* **Stage 3 runs inside Stage 2.** The mechanical gate is a loop with the executor (reject, feed back, retry,
  three times, then record unknown), so a gate span nests in the execute span, one per attempt, and the
  execute span is one per round. A segment runs in a worker thread once its dependencies have nodes; under
  `executor_context = cumulative` the segments run one at a time in plan order, because "the chain so far"
  is only reproducible when the order is fixed.
* **Tools are staged once.** Every segment's tool calls run sequentially through the session before the first
  executor call (the session's registry is not shared between threads), and a re-execution reads the same
  staged files: the tools are deterministic, and a repaired node is a re-reading, not new evidence.
* **The verifier's word is checked too.** A verdict that fails its own structural check is recorded as
  invalid with the harness's reason as its feedback; faulty ids are resolved to segments through any node of
  the chain, so a verifier naming a superseded id still names the right segment, and an id the chain never
  had is dropped and counted.
* **Nothing here computes a verdict from evidence.** The weighted decider is `weights.score` over the nodes;
  the adjudicator's answer is gated exactly as v0's; the final label is a lookup (the adjudicator's verdict
  when a round validated, else the majority over the rounds' candidate labels), and an arm with no
  adjudicator thresholds the weighted score at 0.5, which is the fitted-weights criteria arm of PRD C.
* **The store is the last gate.** When the store refuses a chain the loop marked published, the chain is
  marked unpublished, the refusal joins its problems, and it is stored as the record of the refusal. The
  chain file under `chains/` is written whatever happens, so a budget stop leaves the partial chain behind.
* **Two switches cut calls without loosening the gate.** Under `skip_unmeasured` the harness settles a
  criterion whose feature the staged `cell_features` shows has no value here: the gate allows nothing but
  unknown there, so the node is written deterministically (citing the nearest observation by id when the
  tool gave one), and it still passes the gate like any other node. Under `executor_batch` the first
  construction asks one executor call for every criterion, gates the reply node by node, and sends only the
  refused criteria down the per-segment path at attempt 2. Both are arms to measure (PRD 8.5), off by
  default, so every other arm runs exactly as before.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import importlib
import json
from collections import Counter
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from dataclasses import asdict, dataclass, replace
from pathlib import Path
from typing import Any, Callable

from ..backends.base import ExtractionRequest, ExtractionResponse
from ..ids import sha256_json, short
from ..prospect import tools as T
from ..prospect.criteria import CriteriaSet
from ..prospect.memo import CRITERIA_FILE
from ..runtime.tracing import set_attrs, span
from . import chains as CH
from . import nodegate as G
from . import prompts as PR
from . import v0 as V0
from .arms import Inputs, Switches
from .plan import CELL, bind, check_plan, template_plan
from .session import Session, SessionRefusal
from .v0 import ABSTAIN, NEGATIVE, POSITIVE, VERDICTS
from .wire import (ADJUDICATOR_SCHEMA, FEEDBACK_MAX, MAX_ATTEMPTS, NODE_SCHEMA, NODES_SCHEMA, PLAN_SCHEMA,
                   PLANNERS, SCHEMA_VERSION, VERIFIER_SCHEMA, Chain, Decision, Node, Plan, Segment,
                   VerifierVerdict)

TASK_PLAN = "analyst_v1_plan"
TASK_EXECUTE = "analyst_v1_execute"
TASK_EXECUTE_BATCH = "analyst_v1_execute_batch"
TASK_VERIFY = "analyst_v1_verify"
TASK_ADJUDICATE = "analyst_v1_adjudicate"
#: the `model` of a node the harness wrote itself, so the verifier and the store can tell it from a reading
DETERMINISTIC = "deterministic"

VERIFIERS = ("none", "skeptic")
DECIDERS = ("both", "weighted", "adjudicator")
CONTEXTS = ("independent", "cumulative")
#: the three scores triage compares; they agree when all three sit on one side of the threshold
SCORE_MODELS = ("learned", "effort", "criteria")
THRESHOLD = 0.5
WITHHELD = "effort features are withheld in this arm"
#: the v0-shaped answer a row carries when no adjudicator ruled (a weighted-only arm)
EMPTY_ANSWER: dict[str, Any] = {"verdict": ABSTAIN, "probability": 0.5, "claims": [], "unknown_criteria": [],
                                "absent_criteria": [], "next_observation": "", "rationale": ""}


@dataclass(frozen=True)
class LoopConfig:
    """One arm of the loop: the four roles' models, the switches of PRD 8.5 that are the loop's own (the
    evidence switches live in `Switches`), and how many segments may execute at once."""

    executor_model: str
    verifier_model: str
    adjudicator_model: str
    planner_model: str
    effort: str                              # low | medium | high, for every role unless overridden below
    executor_effort: str | None = None
    verifier_effort: str | None = None
    planner: str = "template"                # template | model
    verifier: str = "skeptic"                # none | skeptic
    rounds: int = 1                          # K: verify rounds, the first construction included
    triage: bool = False
    executor_context: str = "independent"    # independent | cumulative
    decider: str = "both"                    # both | weighted | adjudicator
    segment_workers: int = 4
    retrieval: bool = False
    #: a criterion with no value in the staged `cell_features` is settled unknown by the harness, no call
    skip_unmeasured: bool = False
    #: one executor call over every criterion of the first construction instead of one per segment
    executor_batch: bool = False
    prompt_version: str = PR.PROMPT_VERSION

    def __post_init__(self) -> None:
        for name, value, allowed in (("planner", self.planner, PLANNERS), ("verifier", self.verifier, VERIFIERS),
                                     ("decider", self.decider, DECIDERS),
                                     ("executor_context", self.executor_context, CONTEXTS)):
            if value not in allowed:
                raise ValueError(f"{name}: {value!r} is not one of {allowed}")
        for name, flag in (("skip_unmeasured", self.skip_unmeasured), ("executor_batch", self.executor_batch)):
            if not isinstance(flag, bool):
                raise ValueError(f"{name}: {flag!r} is not True or False")
        if self.rounds < 1:
            raise ValueError(f"rounds: {self.rounds} (the first construction is a round, so at least 1)")
        if self.segment_workers < 1:
            raise ValueError(f"segment_workers: {self.segment_workers} (at least 1)")

    def models(self) -> dict[str, str]:
        return {"executor": self.executor_model, "verifier": self.verifier_model, "adjudicator": self.adjudicator_model}


@dataclass(frozen=True)
class _Call:
    """What every model call contributes to the row: its cost, its duration, and whether the cache served it."""

    cost_usd: float
    duration_s: float
    from_cache: bool


@dataclass
class _Executed:
    """What one segment's worker hands back: the node to store and the bookkeeping the main thread adds up,
    so no counter is shared between threads."""

    node: Node
    calls: list[_Call]
    attempts: int
    rejections: int
    skipped: bool = False   # settled by the harness under `skip_unmeasured`: no call and no attempt


def _account(resp: ExtractionResponse) -> _Call:
    return _Call(float(resp.cost_usd or 0.0), float(resp.duration_s or 0.0), bool(getattr(resp, "from_cache", False)))


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _majority(labels: list[str]) -> str | None:
    """STA-CoT's fallback vote over the rounds: the label most rounds gave, none on a tie."""
    counts = Counter(labels).most_common()
    if not counts or (len(counts) > 1 and counts[0][1] == counts[1][1]):
        return None
    return counts[0][0]


def _label_from_score(score: float | None) -> str:
    """The label of an arm with no adjudicator: the weighted score at the threshold, an abstention when the
    weights could not score the chain (too little known, which is unknown, not against)."""
    if score is None:
        return ABSTAIN
    return POSITIVE if score >= THRESHOLD else NEGATIVE


class _Loop:
    """One run of the loop over one cell: the stages as methods, the chain and the counts as state."""

    def __init__(self, backend: Any, session: Session, card_path: Path | None, inputs: Inputs, switches: Switches,
                 cfg: LoopConfig, criteria: CriteriaSet, chain_id: str, run_id: str,
                 log: Callable[[str], None] | None):
        self.backend, self.session, self.switches, self.cfg, self.criteria = backend, session, switches, cfg, criteria
        self.images: tuple[Path, ...] = (card_path,) if inputs.card and card_path else ()
        self.log = log or (lambda _line: None)
        self.chain = Chain(chain_id=chain_id, cell=session.bench_id, purpose=session.purpose,
                           plan=Plan(planner="template", segments=[], prompt_version=cfg.prompt_version), run_id=run_id)
        #: the latest result per tool the loop itself asked for: triage's flags, the gate's coverage, the effort note
        self.served: dict[str, T.ToolResult] = {}
        self.files: dict[str, list[str]] = {}        # segment -> the staged files its executor reads
        self.refused: dict[str, list[str]] = {}      # segment -> "tool: rule" for every call the session refused
        self.calls: list[_Call] = []
        self.counts: Counter[str] = Counter()
        self.stages: dict[str, Any] = {}
        self.next_node = 1
        self.valid = False
        self.answer: dict[str, Any] | None = None
        self.problems: list[str] = []                # the adjudicator's gate problems, then the store's refusal
        self.adjudicator: ExtractionResponse | None = None
        self.decision_row: dict[str, Any] | None = None
        self.decision_published = False
        self.adjudicator_attempts = 0
        self.effort: str | None = None               # the effort note, built once: a refusal is not asked twice

    # ---------------------------------------------------------------- shared

    def _call(self, tool: str, args: dict[str, Any]) -> T.ToolResult:
        """One tool call through the session, `$cell` bound to the id the model knows the cell by (the session
        maps that id, or the placeholder, to the real cell; a benchmark session refuses the real id)."""
        result = self.session.call(tool, bind(args, self.session.bench_id))
        self.served[tool] = result
        return result

    def _request(self, task: str, system: str, user: str, schema: dict[str, Any], model: str, effort: str,
                 context: dict[str, Any], stage_files: tuple[tuple[Path, str], ...] = (),
                 images: tuple[Path, ...] = ()) -> ExtractionRequest:
        """A request whose cache key carries the cell, the switches and the role's own context, so two arms
        over one cell, or two attempts at one segment, never share an answer."""
        return ExtractionRequest(
            task=task, images=images, stage_files=stage_files, system_prompt=system, user_prompt=user,
            schema=schema, schema_version=SCHEMA_VERSION, prompt_version=self.cfg.prompt_version, model=model,
            effort=effort,
            context_hash=short(sha256_json({"bench_id": self.session.bench_id, "switches": asdict(self.switches),
                                            **context})),
        )

    def _coverage_unknown(self) -> set[str]:
        """The criterion features with no value at this cell, from the `cell_features` result the session
        served: a row with a null value and no text class, or no row at all. Empty until that tool has been
        served, and then the gate's word on where `unknown` is allowed."""
        feats = self.served.get("cell_features")
        if feats is None:
            return set()
        rows = {str(r.get("feature")): r for r in feats.rows}
        out: set[str] = set()
        for c in self.criteria.criteria:
            row = rows.get(c.feature)
            if row is None or (row.get("value") is None and not row.get("text")):
                out.add(c.feature)
        return out

    def _effort_note(self) -> str:
        """One sentence on the effort null for the verifier and the adjudicator, with the value id of the one
        number in it, built once per cell. The score is served here when the plan never asked for it (the
        template never does); the session enforces the switch and the fold, so a refusal is the honest note,
        not a silent gap, and it is not asked again by the next role."""
        if self.effort is None:
            self.effort = self._build_effort_note()
        return self.effort

    def _build_effort_note(self) -> str:
        if "cell_scores" not in self.served:
            try:
                self._call("cell_scores", {"cell_id": CELL})
            except SessionRefusal as refusal:
                return WITHHELD if refusal.rule == "switch" else f"no effort score was served for this cell ({refusal.rule})"
        row = next((r for r in self.served["cell_scores"].rows if r.get("model") == "effort"), None)
        if row is None or row.get("score") is None or not row.get("score_id"):
            return "no effort score is available for this cell"
        return (f"the effort model, fitted on where people looked rather than what is in the rock, scores this "
                f"cell {row['score']} [{row['score_id']}]")

    # ---------------------------------------------------------------- Stage 0

    def triage(self) -> bool:
        """Deterministic and free: the three scores and the cell's coverage decide the depth. Shallow only when
        the scores agree and every counted criterion has a value here that the grid does not call thin;
        scores the session refuses (the switch, or no fold to serve out-of-fold) mean the full chain."""
        with span("stage:triage", kind="tool"):
            try:
                scores = self._call("cell_scores", {"cell_id": CELL})
            except SessionRefusal:
                set_attrs(scores_served=False, shallow=False)
                return False
            coverage = self._call("coverage", {})
            self._call("cell_features", {"cell_id": CELL})
            by_model = {str(r.get("model")): r.get("score") for r in scores.rows}
            got = [by_model.get(m) for m in SCORE_MODELS]
            numbers = [float(s) for s in got if isinstance(s, (int, float)) and not isinstance(s, bool)]
            agree = len(numbers) == len(SCORE_MODELS) and (all(s > THRESHOLD for s in numbers)
                                                           or all(s < THRESHOLD for s in numbers))
            wanted = {c.feature for c in self.criteria.counted}
            thin = {str(r.get("feature")) for r in coverage.rows if r.get("thin")}
            gaps = wanted & (self._coverage_unknown() | thin)
            shallow = agree and not gaps
            set_attrs(scores_served=True, n_scores=len(numbers), scores_agree=agree, n_gaps=len(gaps), shallow=shallow)
            return shallow

    # ---------------------------------------------------------------- Stage 1

    def plan(self) -> None:
        """The template, or a model plan that passed `check_plan`; a model plan that did not falls back to
        the template with its problems recorded, because a cell is never left unplanned for a bad plan."""
        with span("stage:plan", kind="tool", planner=self.cfg.planner):
            plan = template_plan(self.criteria, self.switches, retrieval=self.cfg.retrieval)
            if self.cfg.planner == "model":
                proposed, problems = self._model_plan()
                if proposed is not None:
                    plan = proposed
                else:
                    self.stages["planner_fallback"] = problems
            self.chain.plan = plan
            set_attrs(n_segments=len(plan.segments), planner=plan.planner, fallback="planner_fallback" in self.stages)

    def _model_plan(self) -> tuple[Plan | None, list[str]]:
        if "coverage" not in self.served:
            self._call("coverage", {})
        req = self._request(TASK_PLAN, PR.planner_system(),
                            PR.planner_user(V0._criteria(CRITERIA_FILE.read_text()), list(T.TOOL_HELP.values()),
                                            self._coverage_flags()),
                            PLAN_SCHEMA, self.cfg.planner_model, self.cfg.effort, {"role": "planner"})
        resp = self.backend.call(req)
        self.calls.append(_account(resp))
        try:
            plan = Plan.from_dict({"planner": "model", "prompt_version": self.cfg.prompt_version,
                                   "segments": list((resp.structured or {}).get("segments") or [])})
        except (ValueError, TypeError) as err:
            return None, [str(err)]
        problems = check_plan(plan, self.criteria, self.switches)
        return (None, problems) if problems else (plan, [])

    def _coverage_flags(self) -> dict[str, str]:
        """Per counted criterion feature, for the planner: unmeasured here (no value at this cell, when
        `cell_features` was served), else the grid's word from `coverage`: unmeasured, thin or measured."""
        cov = {str(r.get("feature")): r for r in self.served["coverage"].rows}
        unknown_here = self._coverage_unknown()
        flags: dict[str, str] = {}
        for c in self.criteria.counted:
            row = cov.get(c.feature)
            if c.feature in unknown_here:
                flags[c.feature] = "unmeasured here"
            elif row is None or not row.get("coverage"):
                flags[c.feature] = "unmeasured"
            else:
                flags[c.feature] = "thin" if row.get("thin") else "measured"
        return flags

    # ---------------------------------------------------------------- Stages 2 and 3

    def execute(self, segments: list[Segment], round: int, notes: dict[str, str]) -> None:
        """One round of Stage 2 with Stage 3 inside it. `segments` is the whole plan in round 0 and the
        faulted closure after; `notes` is what a re-executed segment is told first. What the harness can
        settle without a worker is settled first: a criterion with no value here under `skip_unmeasured`,
        and in round 0 under `executor_batch` every criterion the one batch call answered to the gate's
        satisfaction. The rest run in a pool once their dependencies have nodes; the nodes are stored as they
        finish, so a budget stop mid-round leaves what was done, and are put in plan order when the round
        completes."""
        plan = self.chain.plan
        order = {s.segment_id: i for i, s in enumerate(plan.segments)}
        with span("stage:execute", kind="tool", round=round, n_segments=len(segments)):
            if round == 0:
                self._stage_tools(segments)
            targets = {s.segment_id for s in segments}
            done = {n.segment_id: n for n in self.chain.current_nodes() if n.segment_id not in targets}
            ids: dict[str, str] = {}
            for s in sorted(segments, key=lambda s: order[s.segment_id]):
                ids[s.segment_id] = f"n{self.next_node:02d}"
                self.next_node += 1
            workers = 1 if self.cfg.executor_context == "cumulative" else self.cfg.segment_workers
            pending = sorted(segments, key=lambda s: order[s.segment_id])
            start = len(self.chain.nodes)
            tally: Counter[str] = Counter()

            def finish(seg: Segment, out: _Executed) -> None:
                done[seg.segment_id] = out.node
                self.chain.nodes.append(out.node)
                self.calls += out.calls
                tally.update(attempts_total=out.attempts, n_gate_rejections=out.rejections,
                             n_recorded_unknown=int(not out.node.published), n_skipped_unmeasured=int(out.skipped))

            settled: dict[str, _Executed] = {}
            rejected: dict[str, list[str]] = {}
            for s in pending:
                feature = self._unmeasured_feature(s)
                if feature is not None:
                    settled[s.segment_id] = self._settle_unmeasured(s, ids[s.segment_id], round, feature)
            if round == 0 and self.cfg.executor_batch:
                asked = [s for s in pending if s.kind == "criterion" and s.segment_id not in settled]
                if asked:
                    passed, rejected = self._batch(asked, ids, round)
                    settled.update(passed)
            for s in pending:
                if s.segment_id in settled:
                    finish(s, settled[s.segment_id])
            pending = [s for s in pending if s.segment_id not in settled]
            running: dict[Future[_Executed], Segment] = {}
            with ThreadPoolExecutor(max_workers=workers) as pool:
                try:
                    while pending or running:
                        ready = [s for s in pending if all(d in done for d in s.depends_on)][:workers - len(running)]
                        for s in ready:
                            pending.remove(s)
                            fut = pool.submit(contextvars.copy_context().run, self._segment, s, ids[s.segment_id],
                                              round, self._prior(s, done, order), notes.get(s.segment_id),
                                              rejected.get(s.segment_id))
                            running[fut] = s
                        if not running:
                            raise RuntimeError(f"segments {[s.segment_id for s in pending]} wait on nodes that never come")
                        finished, _ = wait(list(running), return_when=FIRST_COMPLETED)
                        for fut in finished:
                            finish(running.pop(fut), fut.result())
                except BaseException:
                    for fut in running:
                        fut.cancel()
                    raise
            self.chain.nodes[start:] = sorted(self.chain.nodes[start:], key=lambda n: order[n.segment_id])
            self.counts.update(tally)
            set_attrs(n_staged=sum(len(f) for f in self.files.values()),
                      n_refused=sum(len(r) for r in self.refused.values()),
                      n_published=sum(1 for n in self.chain.nodes[start:] if n.published), **tally)

    def _unmeasured_feature(self, seg: Segment) -> str | None:
        """Under `skip_unmeasured`, the feature a criterion segment would read when the staged `cell_features`
        shows it has no value at this cell; None otherwise. A cross-check is never settled this way: what it
        combines is a question about nodes, not about one feature's coverage."""
        if not self.cfg.skip_unmeasured or seg.kind != "criterion":
            return None
        c = next((c for c in self.criteria.criteria if c.key == seg.criterion), None)
        if c is None or c.feature not in self._coverage_unknown():
            return None
        return c.feature

    def _settle_unmeasured(self, seg: Segment, node_id: str, round: int, feature: str) -> _Executed:
        """The node for a criterion whose feature has no value here, written by the harness: unknown with
        strength 0, citing the nearest observation's id when `cell_features` gave one, so the node can say
        how far the nearest measurement is by id and never by a bare number. The gate's own rule allows
        unknown exactly here, and the node still goes through the gate like any other; a node of the
        harness's own that fails it is a bug, not a rejection, so that raises instead of retrying."""
        row = next((r for r in self.served["cell_features"].rows if r.get("feature") == feature), {})
        nearest = row.get("nearest_observation_id")
        cited = [nearest] if isinstance(nearest, str) and nearest else []
        text = f"unmeasured here: {feature} has no value at this cell"
        if cited:
            text += f"; nearest observation {cited[0]}"
        node = Node(node_id=node_id, segment_id=seg.segment_id, kind=seg.kind, criterion=seg.criterion,
                    status="unknown", strength=0, value_ids=cited, text=text,
                    unknown_reason=f"no value for {feature} at this cell", round=round, attempt=1,
                    model=DETERMINISTIC, published=True)
        problems = G.check_node(node, self.session.values, self.session.context, self.criteria,
                                {self.session.bench_id}, self._coverage_unknown())
        if problems:
            raise RuntimeError(f"the harness's unmeasured node for {seg.segment_id} failed the gate: {problems}")
        return _Executed(node, [], 0, 0, skipped=True)

    def _batch(self, asked: list[Segment], ids: dict[str, str], round: int) -> tuple[dict[str, _Executed],
                                                                                    dict[str, list[str]]]:
        """One executor call over every criterion segment at once, the single-shot executor arm of PRD 8.5,
        gated node by node exactly as a per-segment answer is. Returns the segments whose node passed, and
        for each the gate refused (or the reply left out) the gate's reasons, which the per-segment path
        takes up at attempt 2 so only the refused criteria cost another call. The reply is split by
        `criterion`: a node for a criterion not asked for, or a second node for one criterion, is ignored
        and counted. The call is accounted once; its nodes carry no cost of their own, because a share per
        node would be a number the harness made up. No prior nodes are shown whatever `executor_context`
        says: the criteria depend on nothing and a batch has no earlier node to build on."""
        files = list(dict.fromkeys(f for s in asked for f in self.files.get(s.segment_id, [])))
        req = self._request(
            TASK_EXECUTE_BATCH, PR.default_executor_system(),
            PR.executor_batch_user(asked, files, card=bool(self.images)), NODES_SCHEMA, self.cfg.executor_model,
            self.cfg.executor_effort or self.cfg.effort,
            {"executor_batch": True, "segment_ids": [s.segment_id for s in asked], "round": round, "attempt": 1},
            stage_files=tuple((self.session.stage / f, f) for f in files), images=self.images)
        resp = self.backend.call(req)
        self.calls.append(_account(resp))
        self.counts["n_batch_calls"] += 1
        wanted = {s.criterion for s in asked}
        answers: dict[str, dict[str, Any]] = {}
        items = dict(resp.structured or {}).get("nodes")
        for item in items if isinstance(items, list) else []:
            key = item.get("criterion") if isinstance(item, dict) else None
            if key in wanted and key not in answers:
                answers[key] = item
            else:
                self.counts["n_batch_extra"] += 1
        meta = dict(model=resp.model_resolved or req.model, cost_usd=0.0, duration_s=0.0)
        passed: dict[str, _Executed] = {}
        refused: dict[str, list[str]] = {}
        for s in asked:
            payload = answers.get(s.criterion or "")
            with span("stage:gate", kind="gate", round=round, attempt=1, batch=True):
                if payload is None:
                    problems = [f"batch: the reply has no node for {s.criterion}; return one node for it"]
                else:
                    node, problems = self._gate(payload, s, ids[s.segment_id], [], round, 1, meta)
                    if not problems:
                        passed[s.segment_id] = _Executed(replace(node, published=True), [], 1, 0)
                set_attrs(n_problems=len(problems), published=not problems, problems=[p[:160] for p in problems[:3]])
            if problems:
                refused[s.segment_id] = problems
        return passed, refused

    def _stage_tools(self, segments: list[Segment]) -> None:
        """Every segment's tool calls, in order, through the session; a refused call is remembered on the
        segment and skipped, so the executor reads what the arm allows and nothing says what it does not."""
        for seg in segments:
            start = len(self.session.calls)
            for call in seg.tool_calls:
                try:
                    self._call(call["tool"], dict(call.get("args") or {}))
                except SessionRefusal as refusal:
                    self.refused.setdefault(seg.segment_id, []).append(f"{call['tool']}: {refusal.rule}")
            self.files[seg.segment_id] = [c["file"] for c in self.session.calls[start:]]

    def _prior(self, seg: Segment, done: dict[str, Node], order: dict[str, int]) -> list[Node]:
        """What the executor may read besides its staged files: its dependencies' current nodes always (an
        unknown record included, because the gate's final word on a criterion is an answer, not a gap), and
        under cumulative context every published node so far, in plan order."""
        if self.cfg.executor_context == "cumulative":
            keep = [n for sid, n in done.items() if n.published or sid in seg.depends_on]
            return sorted(keep, key=lambda n: order[n.segment_id])
        return [done[d] for d in seg.depends_on]

    def _segment(self, seg: Segment, node_id: str, round: int, prior: list[Node], note: str | None,
                 rejected: list[str] | None = None) -> _Executed:
        """One segment to its stored node: the executor call, the gate, and the escalating retry. `rejected`
        is the gate's word on this segment's node from the batch call when there was one: that call was
        attempt 1, so the path starts at attempt 2 with its feedback. Runs in a worker thread, so it touches
        nothing shared but the backend and the session's static registry."""
        files = self.files.get(seg.segment_id, [])
        depends_on = [n.node_id for n in prior if n.segment_id in seg.depends_on]
        base = PR.executor_user(seg, files, card=bool(self.images), prior_nodes=prior)
        if note:
            base = f"{note}\n\n{base}"
        calls: list[_Call] = []
        problems: list[str] = list(rejected or [])
        node: Node | None = None
        for attempt in range(2 if rejected else 1, MAX_ATTEMPTS + 1):
            user = base if attempt == 1 else f"{base}\n\n{G.feedback(problems, attempt, self.session.allowed_ids())}"
            req = self._request(
                TASK_EXECUTE, PR.default_executor_system(), user, NODE_SCHEMA, self.cfg.executor_model,
                self.cfg.executor_effort or self.cfg.effort,
                {"segment_id": seg.segment_id, "round": round, "attempt": attempt,
                 "executor_context": self.cfg.executor_context},
                stage_files=tuple((self.session.stage / f, f) for f in files), images=self.images)
            resp = self.backend.call(req)
            calls.append(_account(resp))
            meta = dict(model=resp.model_resolved or req.model, cost_usd=float(resp.cost_usd or 0.0),
                        duration_s=float(resp.duration_s or 0.0))
            with span("stage:gate", kind="gate", round=round, attempt=attempt):
                node, problems = self._gate(dict(resp.structured or {}), seg, node_id, depends_on, round, attempt, meta)
                # the first reasons travel with the span, so a run's rejections can be read without the prompts
                set_attrs(n_problems=len(problems), published=not problems, problems=[p[:160] for p in problems[:3]])
            if not problems:
                return _Executed(replace(node, published=True), calls, attempt, attempt - 1)
        assert node is not None
        return _Executed(G.record_unknown(node, problems), calls, MAX_ATTEMPTS, MAX_ATTEMPTS)

    def _gate(self, payload: dict[str, Any], seg: Segment, node_id: str, depends_on: list[str], round: int,
              attempt: int, meta: dict[str, Any]) -> tuple[Node, list[str]]:
        """Stage 3 on one answer (`payload`, the node as the model returned it; `meta`, the model, cost and
        duration the harness attributes to it): the node under the segment's identity and every problem
        with it. A model that answered about another segment is refused by `from_model` before the rules
        run; that is a rejection like any other, and the node it leaves is what the unknown record is made
        from. A place name is refused here too, where the executor can fix it, because every later prompt
        builder would refuse to render the node and nothing at that point can."""
        try:
            node = Node.from_model(payload, node_id, seg, depends_on, round, attempt)
        except ValueError as err:
            blank = Node(node_id=node_id, segment_id=seg.segment_id, kind=seg.kind, criterion=seg.criterion,
                         status="unknown", strength=0, depends_on=list(depends_on), round=round, attempt=attempt)
            return replace(blank, **meta), [str(err)]
        node = replace(node, **meta)
        problems = G.check_node(node, self.session.values, self.session.context, self.criteria,
                                {self.session.bench_id}, self._coverage_unknown())
        leaked = V0.place_names_in(node.text)
        if leaked:
            problems.append(f"closed-book: the text names a place ({', '.join(leaked)}); no place names")
        return node, problems

    # ---------------------------------------------------------------- Stage 4

    def _refine(self) -> bool:
        """Verify, repair, verify again, up to K rounds. True when some round validated the chain."""
        if self.cfg.verifier == "none":
            return True
        round = 0
        while True:
            verdict = self.verify(round)
            if verdict.valid:
                return True
            if round + 1 >= self.cfg.rounds:
                return False
            targets, notes = self._targets(verdict)
            if not targets:
                return False  # nothing to repair: re-verifying the same chain would only repeat the answer
            round += 1
            self.counts["n_reexecuted"] += len(targets)
            self.execute(targets, round, notes)

    def verify(self, round: int) -> VerifierVerdict:
        with span("stage:verify", kind="tool", round=round):
            req = self._request(TASK_VERIFY, PR.verifier_system(),
                                PR.verifier_user(self.chain.chain_text(), self._effort_note()), VERIFIER_SCHEMA,
                                self.cfg.verifier_model, self.cfg.verifier_effort or self.cfg.effort,
                                {"role": "verifier", "round": round})
            resp = self.backend.call(req)
            self.calls.append(_account(resp))
            verdict = replace(self._checked_verdict(dict(resp.structured or {}), round),
                              model=resp.model_resolved or req.model, cost_usd=float(resp.cost_usd or 0.0),
                              duration_s=float(resp.duration_s or 0.0))
            self.chain.verdicts.append(verdict)
            self.counts["n_faulty_total"] += len(verdict.faulty)
            set_attrs(valid=verdict.valid, n_faulty=len(verdict.faulty))
            return verdict

    def _checked_verdict(self, payload: dict[str, Any], round: int) -> VerifierVerdict:
        """The verifier's verdict under the same rule as a node: structurally checked before it is trusted.
        Faulty ids the chain never had are dropped and counted; a verdict that still fails its check is
        recorded as invalid with the harness's reason as its feedback and no repairs, since a verifier that
        cannot state its verdict in the protocol has not faulted anything in particular."""
        verdict = VerifierVerdict.from_model(payload, round)
        known = {n.node_id for n in self.chain.nodes}
        named = [f for f in verdict.faulty if isinstance(f, dict) and f.get("node_id") in known]
        self.counts["n_faulty_unresolved"] += len(verdict.faulty) - len(named)
        verdict.faulty = named
        problems = verdict.check()
        if not problems:
            return verdict
        label = verdict.candidate_label if verdict.candidate_label in VERDICTS else ABSTAIN
        return VerifierVerdict(round=round, valid=False, faulty=[], candidate_label=label,
                               feedback=f"verifier verdict rejected by the harness: {problems[0]}"[:FEEDBACK_MAX])

    def _targets(self, verdict: VerifierVerdict) -> tuple[list[Segment], dict[str, str]]:
        """The segments a verdict sends back to Stage 2 (the faulted nodes' and, transitively, everything
        that depends on them), in plan order, and what each is told: its own node's reason, or which nodes
        it built on were faulted. A reason that names a place is withheld rather than rendered."""
        plan = self.chain.plan
        seg_of = {n.node_id: n.segment_id for n in self.chain.nodes}
        current = {n.segment_id: n.node_id for n in self.chain.current_nodes()}
        reasons = {seg_of[f["node_id"]]: str(f["reason"]) for f in verdict.faulty}
        targets = set(reasons)
        grown = True
        while grown:
            grown = False
            for s in plan.segments:
                if s.segment_id not in targets and targets & set(s.depends_on):
                    targets.add(s.segment_id)
                    grown = True
        notes: dict[str, str] = {}
        for s in plan.segments:
            if s.segment_id not in targets:
                continue
            if s.segment_id in reasons:
                note = f"The chain verifier faulted your earlier node {current[s.segment_id]} for this segment: {reasons[s.segment_id]}"
            else:
                deps = ", ".join(current[d] for d in s.depends_on if d in targets)
                note = (f"This segment is re-executed because the node(s) it builds on ({deps}) were faulted by the "
                        f"chain verifier and re-executed; read their new lines below.")
            if verdict.feedback:
                note += f"\nChain-level feedback from the verifier: {verdict.feedback}"
            if V0.place_names_in(note):
                note = (f"The chain verifier faulted the earlier node {current[s.segment_id]} for this segment; its "
                        f"reason is withheld because it named a place.")
            notes[s.segment_id] = note
        return [s for s in plan.segments if s.segment_id in targets], notes

    # ---------------------------------------------------------------- Stage 5

    def decide(self, shallow: bool) -> Decision:
        """Both deciders when the arm asks for both, then the label and the probability by lookup. The
        adjudicator's verdict is the label only when a round validated; a chain that never validated takes
        the majority over the rounds' candidate labels and abstains without one."""
        cfg = self.cfg
        with span("stage:decide", kind="tool", decider=cfg.decider, shallow=shallow):
            weighted = version = None
            if cfg.decider in ("both", "weighted") and not shallow:
                weighted, version = self._weighted(self.chain.current_nodes())
            answer = self._adjudicate(shallow) if shallow or cfg.decider in ("both", "adjudicator") else None
            self.decision_published = (not self.problems) if answer is not None else weighted is not None
            majority = abstained = None
            if shallow or self.valid:
                if answer is None:
                    verdict = _label_from_score(weighted)
                elif self.problems:
                    verdict = ABSTAIN
                    abstained = None if shallow else f"adjudicator rejected: {self.problems[0]}"
                else:
                    verdict = str(answer["verdict"])
            else:
                majority = _majority([v.candidate_label for v in self.chain.verdicts])
                verdict = majority if majority is not None else ABSTAIN
                abstained = None if majority is not None else "no round validated and no majority"
            if weighted is not None:
                probability = weighted
            elif answer is not None and not self.problems:
                probability = float(answer["probability"])
            else:
                probability = 0.5
            well_formed = answer is not None and all(k in answer for k in ADJUDICATOR_SCHEMA["required"]) \
                and answer.get("verdict") in VERDICTS
            decision = Decision(final_verdict=verdict, final_probability=probability, weighted_score=weighted,
                                weights_version=version, adjudicator=answer if well_formed else None,
                                majority_label=majority, abstained_reason=abstained)
            set_attrs(verdict=verdict, adjudicated=answer is not None, adjudicator_published=self.decision_published,
                      weighted=weighted is not None, majority=majority is not None)
            return decision

    def _weighted(self, nodes: list[Node]) -> tuple[float | None, str | None]:
        """Decider (a): the weights module's score over the current nodes, with the version of the weights it
        used. Imported here, not at the top, so an arm that never uses it does not need it."""
        try:
            W = importlib.import_module(f"{__package__}.weights")
        except ImportError as err:
            raise ImportError("decider (a) needs legacy_reader.analyst.weights with criteria_weights(criteria) and "
                              "score(nodes, weights); run with decider='adjudicator' until it is there") from err
        weights = W.criteria_weights(self.criteria)
        score = W.score(nodes, weights)
        return (None, None) if score is None else (float(score), str(weights.version))

    def _adjudicate(self, shallow: bool) -> dict[str, Any]:
        """Decider (b): one call over the chain and the effort null (over the staged tool results alone on the
        shallow path), gated as v0's answer is, against the session's registry and context."""
        user = PR.adjudicator_user(self.chain.chain_text(), self._effort_note())
        files: tuple[tuple[Path, str], ...] = ()
        if shallow:
            names = [c["file"] for c in self.session.calls]
            files = tuple((self.session.stage / f, f) for f in names)
            lines = ["There is no chain: rule from the staged tool results. Read these first:"]
            if self.images:
                lines.append(f"  {{STAGE_DIR}}/{V0.CARD_FILE}      the map card of the cell")
            lines += [f"  {{STAGE_DIR}}/{f}      a staged tool result: rows, and the value ids you may cite" for f in names]
            user = "\n".join(lines) + "\n\n" + user
        # The same three attempts with the gate's escalating feedback the executor gets: Opus never needed
        # them, a cheap adjudicator cites a feature name where the id belongs and lost nine chains in 39 to
        # it. The gate is not loosened; the answer is asked for again with what it did wrong.
        base = user
        cost = duration = 0.0
        for attempt in range(1, MAX_ATTEMPTS + 1):
            user = base if attempt == 1 else f"{base}\n\n{G.feedback(self.problems, attempt, self.session.allowed_ids())}"
            req = self._request(TASK_ADJUDICATE, PR.adjudicator_system(), user, ADJUDICATOR_SCHEMA,
                                self.cfg.adjudicator_model, self.cfg.effort,
                                {"role": "adjudicator", "shallow": shallow, "rounds": self.chain.rounds(), "attempt": attempt},
                                stage_files=files, images=self.images if shallow else ())
            resp = self.backend.call(req)
            self.calls.append(_account(resp))
            cost += float(resp.cost_usd or 0.0)
            duration += float(resp.duration_s or 0.0)
            self.adjudicator = resp
            answer = dict(resp.structured or {})
            self.answer = answer
            self.problems = V0.gate(answer, {"values": self.session.values}, context=self.session.context)
            self.adjudicator_attempts = attempt
            if not self.problems:
                break
        cited = sorted({v for c in answer.get("claims") or [] if isinstance(c, dict)
                        for v in (c.get("value_ids") or []) if isinstance(v, str)})
        self.decision_row = {
            "adjudicator_json": answer, "claims_json": list(answer.get("claims") or []),
            "values_json": {v: self.session.values[v] for v in cited if v in self.session.values},
            "published": not self.problems, "problems_json": list(self.problems),
            "model": resp.model_resolved or req.model, "cost_usd": cost,
            "duration_s": duration, "created_at": _now(),
        }
        return answer

    # ---------------------------------------------------------------- Stage 6

    def publish(self, decision: Decision, con: Any, manifest_sha256: str | None, arm: str) -> None:
        """The whole-chain gate, then the store when there is one and the purpose allows it. A store refusal
        of a chain marked published turns it into an unpublished chain with the refusal among its problems,
        stored as the record of the refusal, never dropped."""
        chain = self.chain
        with span("stage:publish", kind="tool"):
            chain.decision = decision
            chain.manifest_sha256 = manifest_sha256
            chain.published = self.valid and self.decision_published and all(n.published for n in chain.current_nodes())
            chain.validate()
            stored = False
            if con is not None and self.session.purpose != "benchmark":
                try:
                    CH.store_chain(con, *self._store_rows(arm, manifest_sha256), context=self.session.context)
                except CH.ChainRefused as err:
                    if not chain.published:
                        raise
                    chain.published = False
                    self.problems.append(f"store: {err}")
                    CH.store_chain(con, *self._store_rows(arm, manifest_sha256), context=self.session.context)
                stored = True
            set_attrs(published=chain.published, stored=stored)

    def _store_rows(self, arm: str, manifest_sha256: str | None) -> tuple[dict[str, Any], list[dict[str, Any]],
                                                                          list[dict[str, Any]], dict[str, Any] | None]:
        """The chain in the store's shape: node ids are the plan position (`n01` for the first segment in every
        round), distinguished by (round, attempt), so the store's `current_nodes` reads the same chain as the
        wire's; every id a node or a verdict names is mapped the same way."""
        chain, s, d = self.chain, self.session, self.chain.decision
        assert d is not None
        base = {seg.segment_id: f"n{i + 1:02d}" for i, seg in enumerate(chain.plan.segments)}
        sid = {n.node_id: base[n.segment_id] for n in chain.nodes}
        now = _now()
        chain_row = {
            "chain_id": chain.chain_id, "cell_id": s.cell_id, "bench_id": s.bench_id if s.purpose == "benchmark" else None,
            "purpose": s.purpose, "run_id": chain.run_id, "arm": arm, "fold": s.fold, "planner": chain.plan.planner,
            "rounds": chain.rounds(), "valid": self.valid, "final_verdict": d.final_verdict,
            "final_probability": d.final_probability, "weighted_score": d.weighted_score,
            "weights_version": d.weights_version, "verifier_label": self._verifier_label(),
            "majority_label": d.majority_label, "abstained_reason": d.abstained_reason, "published": chain.published,
            "models_json": self.cfg.models(), "manifest_sha256": manifest_sha256, "blind_list_hash": s.blind_list_hash,
            "cost_usd": sum(c.cost_usd for c in self.calls), "duration_s": sum(c.duration_s for c in self.calls),
            "created_at": now,
        }
        nodes = [{
            "node_id": sid[n.node_id], "round": n.round, "attempt": n.attempt, "segment_id": n.segment_id,
            "kind": n.kind, "criterion": n.criterion, "status": n.status, "strength": n.strength,
            "value_ids_json": list(n.value_ids), "expert_ids_json": list(n.expert_ids),
            "depends_on_json": [sid[dep] for dep in n.depends_on], "text": n.text, "published": n.published,
            "problems_json": list(n.problems), "model": n.model, "cost_usd": n.cost_usd, "duration_s": n.duration_s,
            "created_at": now,
        } for n in chain.nodes]
        verdicts = [{
            "round": v.round, "valid": v.valid,
            "faulty_json": [{"node_id": sid[f["node_id"]], "reason": f["reason"]} for f in v.faulty],
            "feedback": v.feedback or None, "candidate_label": v.candidate_label,
            "candidate_probability": v.candidate_probability, "rationale": v.rationale or None, "model": v.model,
            "cost_usd": v.cost_usd, "duration_s": v.duration_s, "created_at": now,
        } for v in chain.verdicts]
        return chain_row, nodes, verdicts, self.decision_row

    # ---------------------------------------------------------------- the run and its row

    def run(self, con: Any, manifest_sha256: str | None, arm: str) -> None:
        shallow = self.cfg.triage and self.triage()
        self.stages["shallow"] = shallow
        if shallow:
            self.valid = True  # nothing to verify: the adjudicator rules over the staged results alone
        else:
            self.plan()
            self.execute(self.chain.plan.segments, 0, {})
            self.valid = self._refine()
        decision = self.decide(shallow)
        self.publish(decision, con, manifest_sha256, arm)
        self.log(f"    {self.session.bench_id}  {decision.final_verdict:<22} p={decision.final_probability:.2f}  "
                 f"rounds={self.chain.rounds()} nodes={len(self.chain.nodes)} attempts={self.counts['attempts_total']}  "
                 f"{'published' if self.chain.published else 'NOT published'}")

    def _verifier_label(self) -> str | None:
        return self.chain.verdicts[-1].candidate_label if self.chain.verdicts else None

    def row(self) -> dict[str, Any]:
        """The v0-shaped row the harness appends and the scorer reads, with the chain and the per-stage
        counts beside it. The answer is the adjudicator's, its verdict and probability replaced by the final
        ones and the adjudicator's own probability kept under its own name."""
        d = self.chain.decision
        assert d is not None
        answer = {**EMPTY_ANSWER, **(self.answer or {})}
        answer.update(verdict=d.final_verdict, probability=d.final_probability,
                      adjudicator_probability=(self.answer or {}).get("probability"), weighted_score=d.weighted_score,
                      weights_version=d.weights_version, majority_label=d.majority_label,
                      verifier_label=self._verifier_label())
        adj = self.adjudicator
        return {
            "bench_id": self.session.bench_id, "answer": answer, "problems": list(self.problems),
            "published": self.chain.published,
            "cost_usd": sum(c.cost_usd for c in self.calls), "duration_s": sum(c.duration_s for c in self.calls),
            "model_resolved": adj.model_resolved if adj is not None else None,
            "cache_key": adj.cache_key if adj is not None else None,
            "from_cache": bool(self.calls) and all(c.from_cache for c in self.calls),
            "chain": self.chain.as_dict(), "stages": self._stages(d),
        }

    def _stages(self, d: Decision) -> dict[str, Any]:
        label = self._verifier_label()
        c = self.counts
        return {
            "n_segments": len(self.chain.plan.segments), "n_nodes": len(self.chain.nodes),
            "attempts_total": c["attempts_total"], "n_gate_rejections": c["n_gate_rejections"],
            "adjudicator_attempts": self.adjudicator_attempts,
            "n_recorded_unknown": c["n_recorded_unknown"], "rounds": self.chain.rounds(), "valid": self.valid,
            "n_faulty_total": c["n_faulty_total"], "n_faulty_unresolved": c["n_faulty_unresolved"],
            "n_reexecuted": c["n_reexecuted"], "n_refusals_by_rule": self.session.manifest_fields()["refusals"],
            "n_skipped_unmeasured": c["n_skipped_unmeasured"], "n_batch_calls": c["n_batch_calls"],
            "n_batch_extra": c["n_batch_extra"],
            "verifier_agreement": None if label is None else label == d.final_verdict,
            "decider_agreement": None if d.weighted_score is None
            else (d.weighted_score >= THRESHOLD) == (d.final_verdict == POSITIVE),
            **self.stages,
        }


def run_cell_v1(
    backend: Any, session: Session, card_path: Path | None, inputs: Inputs, switches: Switches, cfg: LoopConfig,
    criteria: CriteriaSet, *, chain_id: str, run_id: str, con: Any = None, stage: Path | None = None,
    log: Callable[[str], None] | None = None, manifest_sha256: str | None = None, arm: str = "v1",
) -> dict[str, Any]:
    """The staged loop over one cell, on an open session. Returns the row (`_Loop.row`). `BudgetExhausted`,
    `UsageLimitReached` and `BackendError` propagate for the harness to decide; whatever the chain held by
    then is written to `<stage.parent>/chains/<bench id>.json` first, as every finished chain is."""
    loop = _Loop(backend, session, card_path, inputs, switches, cfg, criteria, chain_id, run_id, log)
    try:
        loop.run(con, manifest_sha256, arm)
    finally:
        if stage is not None:
            path = stage.parent / "chains" / f"{session.bench_id}.json"
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(loop.chain.as_dict(), indent=1) + "\n")
    return loop.row()
