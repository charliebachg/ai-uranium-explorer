"""A tool world and a scripted backend for the staged loop tests.

The world serves every tool the template plan calls (`cell_features`, `criteria_breakdown`, `coverage`,
`nearby`, `crosscheck`, `cell_scores`) for one cell, with a value for every counted criterion's feature so a
default node passes the mechanical gate, one feature unmeasured by default so the chain has a real unknown,
and knobs (`scores`, `unmeasured`, `thin`) for what triage reads. The backend answers by role (`req.task`)
and, for the executor, by the segment named in the prompt; a test scripts the exceptions (a bad node, an
invalid verdict, an exception on the nth call) and the defaults answer the rest.
"""

from __future__ import annotations

import re
from collections import defaultdict
from dataclasses import dataclass, field
from typing import Any, Callable

from legacy_reader.analyst.loop import TASK_ADJUDICATE, TASK_EXECUTE, TASK_EXECUTE_BATCH, TASK_PLAN, TASK_VERIFY
from legacy_reader.analyst.v0 import POSITIVE
from legacy_reader.backends.base import ExtractionRequest, ExtractionResponse
from legacy_reader.prospect.tools import ToolResult
from legacy_reader.values import stat

CELL = "0000_0000"
#: (feature, value, unit, fmt): every counted criterion's feature, then the two effort counts
FEATURES: tuple[tuple[str, float | int, str | None, str], ...] = (
    ("d_conductor_m", 820.0, "m", "m1"), ("graphitic_host", 1, None, "int"), ("d_fault_m", 1400.0, "m", "m1"),
    ("fault_density", 0.8, None, "m2"), ("unconformity_depth_m", 512.7, "m", "m1"),
    ("sed_u_max_ppm", 31.5, "ppm", "m1"), ("water_u_max_ppm", 4.2, "ppm", "m1"), ("boulder_max_cps", 900, "cps", "m1"),
    ("holes_n", 12, None, "int"), ("sed_samples_n", 9, None, "int"),
)
EFFORT = {"holes_n", "sed_samples_n"}
#: criterion key -> (feature, membership): the memberships the fake breakdown serves
CRITERIA: dict[str, tuple[str, float]] = {
    "conductor_proximity": ("d_conductor_m", 0.93), "graphitic_host": ("graphitic_host", 1.0),
    "fault_proximity": ("d_fault_m", 0.8), "structural_density": ("fault_density", 0.7),
    "unconformity_depth": ("unconformity_depth_m", 1.0), "lake_sediment_uranium": ("sed_u_max_ppm", 0.6),
    "lake_water_uranium": ("water_u_max_ppm", 0.3), "boulder_train": ("boulder_max_cps", 0.7),
}


def vid(feature: str, cell: str = CELL) -> str:
    return f"c:cell:{cell}:{feature}"


def mid(key: str, cell: str = CELL) -> str:
    return f"c:crit:{cell}:{key}"


def xid(pair: str, what: str, cell: str = CELL) -> str:
    return f"c:x:{cell}:{pair}:{what}"


COND, FAULT, SED, SAMPLES = vid("d_conductor_m"), vid("d_fault_m"), vid("sed_u_max_ppm"), vid("sed_samples_n")


@dataclass
class LoopWorld:
    """What the fakes serve and what they were asked."""

    scores: dict[str, float | None] = field(default_factory=lambda: {"learned": 0.7, "effort": 0.8, "criteria": 0.65})
    unmeasured: set[str] = field(default_factory=lambda: {"water_u_max_ppm"})
    #: metres to the nearest observation of an unmeasured feature, when the grid knows one (as the real tool
    #: serves it, under its own value id)
    nearest: dict[str, float] = field(default_factory=dict)
    thin: set[str] = field(default_factory=set)
    received: dict[str, list[dict[str, Any]]] = field(default_factory=lambda: defaultdict(list))


def loop_registry(world: LoopWorld) -> dict[str, Callable[..., ToolResult]]:
    def cell_features(cell_id: str) -> ToolResult:
        world.received["cell_features"].append({"cell_id": cell_id})
        out = ToolResult("cell_features", {"cell_id": cell_id})
        for feature, value, unit, fmt in FEATURES:
            row: dict[str, Any] = {"feature": feature, "is_effort": feature in EFFORT, "unit": unit}
            if feature in world.unmeasured:
                row.update(value=None, observations=0)
                if feature in world.nearest:
                    nid = f"{vid(feature, cell_id)}:nearest_m"
                    row["nearest_observation_id"] = nid
                    out.values[nid] = stat(nid, world.nearest[feature], fmt="m1", unit="m",
                                           note=f"distance from cell {cell_id} to the nearest {feature} observation")
            else:
                v = vid(feature, cell_id)
                row.update(value=value, observations=3, value_id=v)
                out.values[v] = stat(v, value, fmt=fmt, unit=unit, note=f"{feature} for cell {cell_id}")
            out.rows.append(row)
        out.note = "Effort features describe where people looked, not what is in the rock."
        return out

    def criteria_breakdown(cell_id: str) -> ToolResult:
        world.received["criteria_breakdown"].append({"cell_id": cell_id})
        out = ToolResult("criteria_breakdown", {"cell_id": cell_id})
        for key, (feature, membership) in CRITERIA.items():
            row: dict[str, Any] = {"criterion": key, "weight": 2.0, "status": "assumed"}
            if feature in world.unmeasured:
                row.update(state="unknown", membership=None)
            else:
                m = mid(key, cell_id)
                row.update(state="met" if membership >= 0.5 else "not_met", membership=membership, membership_id=m)
                out.values[m] = stat(m, membership, fmt="ratio3", note=f"membership of {key} at cell {cell_id}")
            out.rows.append(row)
        return out

    def coverage(feature_key: str | None = None) -> ToolResult:
        world.received["coverage"].append({"feature_key": feature_key})
        out = ToolResult("coverage", {"feature_key": feature_key})
        for feature, _value, _unit, _fmt in FEATURES:
            if feature_key and feature != feature_key:
                continue
            cid = f"c:cov:{feature}"
            out.rows.append({"feature": feature, "coverage_id": cid, "coverage": 0.9, "thin": feature in world.thin,
                             "is_effort": feature in EFFORT})
            out.values[cid] = stat(cid, 0.9, fmt="ratio3", note=f"share of cells with an observation behind {feature}")
        return out

    def nearby(cell_id: str, layer: str, radius_m: float = 5000.0, k: int = 5) -> ToolResult:
        world.received["nearby"].append({"cell_id": cell_id, "layer": layer, "radius_m": radius_m, "k": k})
        cid = f"c:nearby:{cell_id}:{layer}:count"
        out = ToolResult("nearby", {"cell_id": cell_id, "layer": layer, "radius_m": radius_m, "k": k})
        out.rows = [{"layer": layer, "count": 4, "count_id": cid, "is_effort": layer == "compilation"}]
        out.values = {cid: stat(cid, 4, note=f"{layer} features within {radius_m:g} m of cell {cell_id}")}
        return out

    def crosscheck(cell_id: str) -> ToolResult:
        world.received["crosscheck"].append({"cell_id": cell_id})
        feats = cell_features(cell_id)
        out = ToolResult("crosscheck", {"cell_id": cell_id})

        def echo(row: dict[str, Any], feature: str) -> None:
            v = vid(feature, cell_id)
            if v in feats.values:
                row[feature], row[f"{feature}_id"] = feats.values[v]["value"], v
                out.values[v] = feats.values[v]
            else:
                row[feature] = None

        row: dict[str, Any] = {"pair": "conductor_fault", "radius_m": 5000.0, "radius_m_id": xid("conductor_fault", "radius_m", cell_id)}
        out.values[row["radius_m_id"]] = stat(row["radius_m_id"], 5000.0, fmt="m1", unit="m")
        echo(row, "d_conductor_m")
        echo(row, "d_fault_m")
        row.update(crossings_n=2, crossings_n_id=xid("conductor_fault", "crossings_n", cell_id), state="known")
        out.values[row["crossings_n_id"]] = stat(row["crossings_n_id"], 2, note="conductor-fault crossings within the radius")
        out.rows.append(row)
        row = {"pair": "sediment_sampling"}
        echo(row, "sed_u_max_ppm")
        echo(row, "sed_samples_n")
        row.update(min_samples=5, min_samples_id=xid("sediment_sampling", "min_samples", cell_id), thin_sampling=False,
                   state="known" if row.get("sed_u_max_ppm") is not None else "unknown")
        out.values[row["min_samples_id"]] = stat(row["min_samples_id"], 5, note="fewer samples than this is thin sampling")
        out.rows.append(row)
        return out

    def cell_scores(cell_id: str) -> ToolResult:
        world.received["cell_scores"].append({"cell_id": cell_id})
        out = ToolResult("cell_scores", {"cell_id": cell_id})
        for model, score in world.scores.items():
            row: dict[str, Any] = {"model": model, "in_area_of_applicability": True}
            if score is None:
                row.update(score=None, missing="no model could score this cell")
            else:
                sid = f"c:score:{cell_id}:{model}"
                row.update(score=score, score_id=sid)
                out.values[sid] = stat(sid, score, fmt="ratio3", note=f"{model} score for cell {cell_id}")
            out.rows.append(row)
        return out

    return {"cell_features": cell_features, "criteria_breakdown": criteria_breakdown, "coverage": coverage,
            "nearby": nearby, "crosscheck": crosscheck, "cell_scores": cell_scores}


# ---------------------------------------------------------------- default answers per role

def node_answer(key: str) -> dict[str, Any]:
    """A node that passes the gate for `key` (a criterion key or a cross-check pair) against the world above."""
    answers: dict[str, dict[str, Any]] = {
        "conductor_proximity": dict(status="met", strength=4, value_ids=[COND], text="The nearest mapped conductor is 820 m away."),
        "graphitic_host": dict(status="met", strength=3, value_ids=[vid("graphitic_host")], text="The bedrock here is mapped as a graphitic host unit."),
        "fault_proximity": dict(status="met", strength=3, value_ids=[FAULT], text="The nearest mapped fault is 1,400 m away."),
        "structural_density": dict(status="met", strength=2, value_ids=[mid("structural_density")], text="Fault density here sits in the upper part of the basin distribution."),
        "unconformity_depth": dict(status="met", strength=3, value_ids=[vid("unconformity_depth_m")], text="The unconformity is interpolated at 512.7 m depth."),
        "lake_sediment_uranium": dict(status="met", strength=2, value_ids=[SED, mid("lake_sediment_uranium")], text="Lake sediment reaches 31.5 ppm within reach."),
        "lake_water_uranium": dict(status="unknown", strength=0, value_ids=[], text="No lake-water sample within reach of this cell.", unknown_reason="water_u_max_ppm has no value here"),
        "boulder_train": dict(status="met", strength=2, value_ids=[mid("boulder_train")], text="Boulder counts here sit above the basin background."),
        "conductor_fault": dict(status="met", strength=3, value_ids=[COND, FAULT, xid("conductor_fault", "crossings_n")], text="The conductor at 820 m and the fault at 1,400 m cross inside the check radius."),
        "sediment_sampling": dict(status="met", strength=2, value_ids=[SED, SAMPLES], text="The lake-sediment reading of 31.5 ppm rests on 9 samples within reach."),
    }
    return {"criterion": key, "expert_ids": [], **answers[key]}


def bad_node() -> dict[str, Any]:
    """The MineTRACE failure for the conductor segment: a number from nowhere."""
    return {**node_answer("conductor_proximity"), "text": "The conductor is 820 m away and grades reached 2.4% U3O8."}


def verifier_answer(valid: bool = True, faulty: list[dict[str, str]] | None = None, feedback: str = "",
                    label: str = POSITIVE, probability: float = 0.7) -> dict[str, Any]:
    return {"valid": valid, "faulty": list(faulty or []), "feedback": feedback, "candidate_label": label,
            "candidate_probability": probability, "rationale": "The nodes agree." if valid else "A node does not hold."}


def adjudicator_answer(verdict: str = POSITIVE, probability: float = 0.8) -> dict[str, Any]:
    return {
        "verdict": verdict, "probability": probability,
        "claims": [{"text": "The nearest mapped conductor is 820 m away.", "value_ids": [COND]}],
        "unknown_criteria": ["lake_water_uranium"], "absent_criteria": [],
        "next_observation": "A ground EM line across the corridor.",
        "rationale": "A conductor inside the corridor distance; lake water is unknown here.",
    }


def bad_adjudicator() -> dict[str, Any]:
    a = adjudicator_answer()
    a["claims"] = [{"text": "The conductor is 820 m away and grades reached 2.4% U3O8.", "value_ids": [COND]}]
    return a


class LoopBackend:
    """Answers by role and, for the executor, by segment. `executor` maps a segment id to a queue of answers
    (or exceptions) consumed one per call, `verifier` and `adjudicator` are queues per call; the defaults
    answer whatever is not scripted. `raise_on_executor_call` raises `error` on that executor call (1-based).
    `batch` scripts the one batch-executor reply per criterion: a node in place of the default answer, None
    to leave that criterion out, and a key the prompt never asked for is appended as an extra node."""

    family = "claude_cli"
    SEGMENT = re.compile(r"^Segment (s\d+): (\w+)(?: (\S+))?\.", re.M)

    def __init__(self, executor: dict[str, list[Any]] | None = None, verifier: list[Any] | None = None,
                 adjudicator: list[Any] | None = None, planner: dict[str, Any] | None = None,
                 raise_on_executor_call: int | None = None, error: BaseException | None = None, cost: float = 0.05,
                 batch: dict[str, Any] | None = None):
        self.executor = {k: list(v) for k, v in (executor or {}).items()}
        self.verifier = list(verifier or [])
        self.adjudicator = list(adjudicator or [])
        self.planner = planner
        self.batch = dict(batch or {})
        self.raise_on_executor_call, self.error = raise_on_executor_call, error
        self.cost = cost
        self.requests: list[ExtractionRequest] = []
        self.calls: list[tuple[str, str | None]] = []   # (task, segment id)
        self.n_executor = 0

    def executor_requests(self, segment_id: str) -> list[ExtractionRequest]:
        return [r for (task, seg), r in zip(self.calls, self.requests) if task == TASK_EXECUTE and seg == segment_id]

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        seg = None
        if req.task == TASK_EXECUTE:
            m = self.SEGMENT.search(req.user_prompt)
            assert m, req.user_prompt[:120]
            seg = m.group(1)
            self.n_executor += 1
            queue = self.executor.get(seg)
            out = queue.pop(0) if queue else node_answer(m.group(3) or m.group(2))
            if self.n_executor == self.raise_on_executor_call:
                out = self.error
        elif req.task == TASK_EXECUTE_BATCH:
            asked = [crit or kind for _sid, kind, crit in self.SEGMENT.findall(req.user_prompt)]
            assert asked, req.user_prompt[:120]
            nodes = [self.batch[k] if k in self.batch else node_answer(k) for k in asked]
            nodes += [v for k, v in self.batch.items() if k not in asked]
            out = {"nodes": [n for n in nodes if n is not None]}
        elif req.task == TASK_VERIFY:
            out = self.verifier.pop(0) if self.verifier else verifier_answer()
        elif req.task == TASK_ADJUDICATE:
            out = self.adjudicator.pop(0) if self.adjudicator else adjudicator_answer()
        elif req.task == TASK_PLAN:
            out = self.planner
        else:
            raise AssertionError(f"unexpected task {req.task!r}")
        self.requests.append(req)
        self.calls.append((req.task, seg))
        if isinstance(out, BaseException):
            raise out
        return ExtractionResponse(structured=out, envelope={}, backend="scripted", backend_version="0",
                                  model_requested=req.model, model_resolved=f"{req.model}-resolved", num_turns=1,
                                  duration_s=1.0, usage={"output_tokens": 40}, cost_usd=self.cost,
                                  cache_key=req.cache_key(self.family))
