"""A six-cell benchmark on disk in the builder's own layout, a scripted analyst backend, and a manifest
stand-in with the runtime's interface.

Shared by the analyst tests so that every one of them runs against the same frozen layout: five open cells
(two deposits, an occurrence, a negative, a probe) and one held-out negative that must never be called. The
packs are built with every pack switch on, so an arm has something to remove; the cards are built without
drillholes, so an arm that asks for them is refused.
"""

from __future__ import annotations

import itertools
import json
import re
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from legacy_reader.backends.base import ExtractionRequest, ExtractionResponse
from legacy_reader.ids import sha256_file
from legacy_reader.runtime.spend import BudgetExhausted, RunBudget  # noqa: F401  (re-exported for the tests)

VERSION = "vtest"

#: bench_id -> (cell_id, label, fold, split)
CELLS: dict[str, tuple[str, str, int, str]] = {
    "b01": ("0001_0001", "deposit", 0, "open"),
    "b02": ("0001_0002", "deposit", 1, "open"),
    "b03": ("0001_0003", "occurrence", 0, "open"),
    "b04": ("0001_0004", "negative", 1, "open"),
    "b05": ("0001_0005", "probe", 0, "open"),
    "b06": ("0001_0006", "negative", 1, "heldout"),
}
OPEN = ["b01", "b02", "b03", "b04", "b05"]
LABELLED = ["b01", "b02", "b03", "b04"]
BUILT_SWITCHES = {"label_context": True, "oof_scores": True, "effort_features": True}


def val(vid: str, value: Any, fmt: str = "m1", unit: str | None = None, note: str | None = None) -> dict[str, Any]:
    v: dict[str, Any] = {"id": vid, "kind": "stat", "as_printed": None, "value": value, "unit_as_printed": None, "fmt": fmt}
    if unit:
        v["unit"] = unit
    if note:
        v["note"] = note
    return v


def ids_for(bench_id: str) -> dict[str, str]:
    return {"cond": f"b:{bench_id}:cell:d_conductor_m", "holes": f"b:{bench_id}:cell:holes_n",
            "near": f"b:{bench_id}:near:0", "score": f"b:{bench_id}:score:learned",
            "crit": f"b:{bench_id}:crit:conductor_proximity"}


def make_pack(bench_id: str) -> dict[str, Any]:
    """The builder's shape, with every kind of row a switch can remove, so an ablation is visible in what is left."""
    i = ids_for(bench_id)
    return {
        "bench_id": bench_id, "version": VERSION, "switches": dict(BUILT_SWITCHES),
        "tools": {
            "cell_features": {"note": "provincial survey 1975-1978", "rows": [
                {"feature": "d_conductor_m", "value": 820.0, "unit": "m", "observations": 3, "value_id": i["cond"]},
                {"feature": "holes_n", "value": 12, "unit": None, "observations": 1, "value_id": i["holes"], "is_effort": True},
                {"feature": "water_u_max_ppm", "value": None, "unit": "ppm", "observations": 0},
            ]},
            "criteria_breakdown": {"note": "", "rows": [
                {"criterion": "conductor_proximity", "state": "met", "membership": 0.9, "weight": 3.0, "status": "assumed",
                 "membership_id": i["crit"]},
                {"criterion": "lake_water_uranium", "state": "unknown", "membership": None, "weight": 1.0, "status": "assumed"},
            ]},
            "coverage": {"note": "", "rows": [
                {"feature": "d_conductor_m", "coverage": 1.0, "thin": False},
                {"feature": "holes_n", "coverage": 1.0, "thin": False, "is_effort": True},
            ]},
            "label_context": {"note": "", "rows": [
                {"rank": 1, "tier": "deposit", "distance_km": 12.4, "distance_km_id": i["near"]}]},
            "cell_scores": {"note": "", "rows": [
                {"model": "learned", "fold": 0, "out_of_fold": True, "score": 0.61, "score_id": i["score"]}]},
        },
        "values": {
            i["cond"]: val(i["cond"], 820.0, unit="m"),
            i["holes"]: val(i["holes"], 12, fmt="int"),
            i["near"]: val(i["near"], 12.4, fmt="m2", unit="km"),
            i["score"]: val(i["score"], 0.61, fmt="ratio3"),
            i["crit"]: val(i["crit"], 0.9, fmt="ratio3"),
        },
        "text": f"Pre-rendered text for {bench_id}: holes_n 12, learned 0.61, nearest label 12.4 km.",
    }


def make_bench(root: Path, version: str = VERSION, card_drillholes: bool = False) -> SimpleNamespace:
    """Write the layout under root/bench/<version>/ and return what the tests need to know about it."""
    d = root / "bench" / version
    for sub in ("packs", "cards", "blind", "passages"):
        (d / sub).mkdir(parents=True, exist_ok=True)
    key: dict[str, dict[str, Any]] = {}
    lines = []
    for bench_id, (cell_id, label, fold, split) in CELLS.items():
        lines.append(json.dumps({"bench_id": bench_id, "cell_id": cell_id, "stratum": label, "fold": fold, "split": split}))
        key[bench_id] = {"label": label, "stratum": label, "fold": fold, "split": split}
        (d / "packs" / f"{bench_id}.json").write_text(json.dumps(make_pack(bench_id), indent=1))
        (d / "cards" / f"{bench_id}.png").write_bytes(b"\x89PNG\r\n\x1a\n" + bench_id.encode() * 16)
        (d / "blind" / f"{bench_id}.json").write_text(json.dumps({"bench_id": bench_id, "radius_km": 10.0, "files": ["74H09-0039"]}))
    (d / "passages" / "b01.json").write_text(json.dumps({"bench_id": "b01", "passages": [
        {"file": "[redacted]", "page": 3, "tier": "read", "text": "The 1975-1978 survey covers this ground."}]}))
    (d / "cells.jsonl").write_text("\n".join(lines) + "\n")
    (d / "key.json").write_text(json.dumps(key, indent=1))
    (d / "heldout.json").write_text(json.dumps(["b06"]))
    skip = {"manifest.json", "key.json"}
    files = {p.relative_to(d).as_posix(): sha256_file(p) for p in sorted(d.rglob("*")) if p.is_file() and p.name not in skip}
    manifest = {"manifest_version": "bench-manifest/v1", "version": version, "seed": 0,
                "spec": {"pack": dict(BUILT_SWITCHES), "card": {"drillholes": card_drillholes}},
                "key_sha256": sha256_file(d / "key.json"), "files": files}
    (d / "manifest.json").write_text(json.dumps(manifest, indent=1))
    return SimpleNamespace(version=version, dir=d, root=root / "bench", key=key,
                           cells={b: c[0] for b, c in CELLS.items()}, manifest_sha256=sha256_file(d / "manifest.json"))


# ---------------------------------------------------------------- answers and the scripted backend

def good_answer(bench_id: str, verdict: str = "supports_closer_look", probability: float = 0.8) -> dict[str, Any]:
    return {
        "verdict": verdict, "probability": probability,
        "claims": [{"text": "The nearest mapped conductor is 820 m away.", "value_ids": [ids_for(bench_id)["cond"]]}],
        "unknown_criteria": ["lake_water_uranium"], "absent_criteria": [],
        "next_observation": "A ground EM line across the corridor.",
        "rationale": "A conductor inside the corridor distance; the detection criteria are unknown here.",
    }


def bad_answer(bench_id: str) -> dict[str, Any]:
    """The MineTRACE failure: a number from nowhere."""
    a = good_answer(bench_id)
    a["claims"] = [{"text": "The conductor is 820 m away and grades reached 2.4% U3O8.",
                    "value_ids": [ids_for(bench_id)["cond"]]}]
    return a


class AnalystBackend:
    """Answers per bench id from a script: an answer dict, an exception to raise, or the default answer."""

    family = "claude_cli"
    _CELL = re.compile(r"^Cell (\S+)\.")

    def __init__(self, script: dict[str, Any] | None = None, answer: Callable[[str], dict[str, Any]] = good_answer,
                 cost: float = 0.05):
        self.script = dict(script or {})
        self.answer = answer
        self.cost = cost
        self.calls: list[str] = []
        self.requests: list[ExtractionRequest] = []

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        m = self._CELL.match(req.user_prompt)
        assert m, req.user_prompt[:80]
        bench_id = m.group(1)
        self.calls.append(bench_id)
        self.requests.append(req)
        out = self.script.get(bench_id)
        if isinstance(out, BaseException):
            raise out
        if out is None:
            out = self.answer(bench_id)
        return ExtractionResponse(structured=out, envelope={}, backend="scripted", backend_version="0",
                                  model_requested=req.model, model_resolved=req.model, num_turns=3,
                                  duration_s=1.0, usage={"output_tokens": 50}, cost_usd=self.cost,
                                  cache_key=req.cache_key(self.family))


# ---------------------------------------------------------------- the manifest, faked

class FakeManifest:
    """The interface the harness codes against (`runtime.manifest.Manifest`), and a record of every start."""

    counter = itertools.count(1)
    started: list["FakeManifest"] = []

    def __init__(self, kind: str, config: dict, models: dict, seed: int | None, budget_usd: float | None,
                 bench: dict | None, run_id: str | None = None):
        self.kind, self.config, self.models, self.seed, self.budget_usd, self.bench = kind, config, models, seed, budget_usd, bench
        self.run_id = run_id or f"bench-{next(self.counter):03d}"
        self.prompt_hashes: dict[str, str] = {}
        self.schema_hashes: dict[str, str] = {}
        self.scores_seen: list[dict[str, Any]] = []
        self.blind_list_sha256: str | None = None
        self.spent_usd: float = 0.0
        self.finished_at: str | None = None
        self.writes = 0

    @classmethod
    def start(cls, kind: str, config: dict, models: dict[str, str], seed: int | None = None,
              budget_usd: float | None = None, bench: dict | None = None, run_id: str | None = None) -> "FakeManifest":
        m = cls(kind, config, models, seed, budget_usd, bench, run_id)
        cls.started.append(m)
        return m

    def budget(self) -> RunBudget:
        return RunBudget(cap_usd=self.budget_usd, spent_usd=self.spent_usd)

    def write(self, run_dir: Path) -> Path:
        self.writes += 1
        Path(run_dir).mkdir(parents=True, exist_ok=True)
        path = Path(run_dir) / "manifest.json"
        path.write_text(json.dumps({
            "run_id": self.run_id, "kind": self.kind, "config": self.config, "models": self.models,
            "seed": self.seed, "budget_usd": self.budget_usd, "bench": self.bench,
            "prompt_hashes": self.prompt_hashes, "schema_hashes": self.schema_hashes,
            "scores_seen": self.scores_seen, "spent_usd": self.spent_usd, "finished_at": self.finished_at}, indent=1))
        return path

    def finish(self, spent_usd: float) -> None:
        self.spent_usd = float(spent_usd)
        self.finished_at = "now"


def install_runtime(monkeypatch, tmp_path: Path) -> SimpleNamespace:
    """Point the harness at the fake manifest, a temporary runs directory and the temporary benchmark root,
    and record MLflow runs instead of logging them. The budget and its exception are the real ones."""
    from legacy_reader.analyst import frozen as F
    from legacy_reader.analyst import run as RUN
    from legacy_reader.prospect import tracking as TR

    FakeManifest.started = []
    FakeManifest.counter = itertools.count(1)
    logged: list[dict[str, Any]] = []
    monkeypatch.setattr(RUN, "Manifest", FakeManifest)
    monkeypatch.setattr(RUN, "run_dir", lambda run_id: tmp_path / "runs" / run_id)
    monkeypatch.setattr(F, "bench_root", lambda: tmp_path / "bench")
    monkeypatch.setattr(TR, "log_run", lambda name, params, metrics, tags=None, artifacts=None:
                        logged.append({"name": name, "params": params, "metrics": metrics, "tags": tags})
                        or f"mlflow-{len(logged)}")
    return SimpleNamespace(runs=tmp_path / "runs", cache=tmp_path / "cache", logged=logged)
