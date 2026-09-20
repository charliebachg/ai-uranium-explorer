"""The harness: one arm over the open cells of a frozen benchmark, budgeted, resumable, traced and scored.

What it guarantees, in the order a run meets them:

* **Held-out cells are never run.** The benchmark's open cells are the plan; naming a held-out id is refused
  before anything is staged, and a run directory that somehow held one would be refused by the scorer.
* **The run names itself before it spends.** A `Manifest` is started and written before the first call,
  with the arm, the model, the prompt and schema hashes and the benchmark's manifest hash, so an aborted run
  still says what it was.
* **Every finished cell is on disk at once.** One line per cell is appended to `cells.jsonl` as it completes;
  a rerun with `resume=<run id>` carries those rows forward and calls the backend only for the rest.
* **The budget stops it, cleanly.** A `BudgetExhausted` (or a usage limit) stops new calls, lets in-flight
  calls finish, and leaves the remaining cells listed as pending. Spend is what left the cache, not what a
  cache hit once cost.
* **One MLflow run per arm**, with the score as metrics, so the table can cite it.

The runtime pieces are `legacy_reader.runtime`'s: the manifest and its budget, the run directory, the cached
backend's budget check, and tracing. They are bound as module names so a test can stand a fake manifest and a
temporary run directory in their place while the budget and its exception stay real.
"""

from __future__ import annotations

import contextlib
import datetime as dt
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

from ..backends.base import BackendError, UsageLimitReached
from ..backends.cache import CachedBackend
from ..ids import sha256_json
from ..prospect import tracking as TR
from . import frozen as F
from . import score as SC
from . import v0 as V0
from .arms import ArmConfig, load_arm

from ..runtime.manifest import Manifest
from ..runtime.runs import run_dir
from ..runtime.spend import BudgetExhausted, RunBudget

try:
    from ..runtime.tracing import span, trace
except ImportError:  # tracing is optional; a run without it is still a run
    def trace(run_id: str, kind: str, run_dir: Path | None = None, **attrs: Any):  # type: ignore[misc]
        return contextlib.nullcontext()

    def span(name: str, kind: str = "tool", **attrs: Any):  # type: ignore[misc]
        return contextlib.nullcontext()

KIND = "bench"
CELLS_FILE = "cells.jsonl"
Factory = Callable[[ArmConfig], Any]


def cached_backend(inner: Any, budget: RunBudget, estimate_usd: float, root: Path | None = None) -> CachedBackend:
    """The call cache around the arm's backend, charging the run's budget and refusing a live call that could
    cross it. The estimate is the arm's per-call ceiling: the most one call is allowed to cost."""
    return CachedBackend(inner, root=root, run_budget=budget, estimate_usd=estimate_usd)


def _note(manifest: Any, attr: str, mapping: dict[str, Any]) -> None:
    cur = getattr(manifest, attr, None)
    if isinstance(cur, dict):
        cur.update(mapping)


def _extend(manifest: Any, attr: str, items: list[Any]) -> None:
    cur = getattr(manifest, attr, None)
    if isinstance(cur, list):
        cur.extend(x for x in items if x not in cur)


def _append(rd: Path, row: dict[str, Any]) -> None:
    with (rd / CELLS_FILE).open("a") as f:
        f.write(json.dumps(TR.jsonable(row), separators=(",", ":")) + "\n")
        f.flush()


def _flat(score: dict[str, Any]) -> dict[str, float]:
    return {k: float(v) for k, v in score.items() if isinstance(v, (int, float)) and not isinstance(v, bool)}


def _claim(base: str) -> tuple[str, Path]:
    """The run id and its directory, created atomically; a taken directory means a sibling run started in
    the same second, and this run takes the next suffix. Goes through this module's `run_dir` so a test can
    redirect every run."""
    for n in range(1, 1000):
        run_id = base if n == 1 else f"{base}-{n}"
        path = run_dir(run_id)
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return run_id, path
    raise RuntimeError(f"could not claim a run directory under {base}")


def run_arm(
    version: str, arm: ArmConfig | str, backend_factory: Factory, budget_usd: float | None,
    log: Callable[[str], None] = print, cells: list[str] | None = None, workers: int | None = None,
    track: bool = True, resume: str | None = None, seed: int = 0, boot: int = SC.BOOT,
    cache_root: Path | None = None,
) -> dict[str, Any]:
    """One arm over the benchmark's open cells (or the named open cells). Returns the run summary, which is
    also written to `<run_dir>/summary.json` and `data/out/bench/<version>/arms/<arm>.json`."""
    arm = load_arm(arm) if isinstance(arm, str) else arm
    bench = F.load_bench(version)
    if not F.hashed_files(bench):
        log("  the benchmark manifest lists no file hashes; nothing verified")
    drift = F.verify(bench)
    if drift:
        raise ValueError(f"benchmark {version} has drifted from its manifest: {drift[:3]}")
    unfit = F.compatibility(bench, arm.switches)
    if unfit:
        raise ValueError(f"arm {arm.name} cannot run on benchmark {version}: " + "; ".join(unfit))
    planned = bench.require_open(list(cells)) if cells else bench.open_cells()
    started = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")

    manifest = Manifest.start(kind=KIND, config=arm.as_dict(), models={"analyst": arm.model}, seed=seed,
                              budget_usd=budget_usd,
                              bench={"bench_id": version, "manifest_sha256": bench.manifest_sha256})
    # the directory is claimed before anything is written: two arms launched in the same second used to be
    # given one run id and interleaved their rows; the second now gets a -2 suffix on its id and directory
    manifest.run_id, rd = _claim(str(manifest.run_id))
    system = V0.default_system_prompt()
    _note(manifest, "prompt_hashes", {arm.prompt_version: sha256_json(system)})
    _note(manifest, "schema_hashes", {V0.SCHEMA_VERSION: sha256_json(V0.ANSWER_SCHEMA)})
    if arm.switches.oof_scores:
        _extend(manifest, "scores_seen", [{"model_version": m, "fold_kind": SC.FOLD_KIND} for m in SC.BASELINE_MODELS])
    run_id = str(manifest.run_id)
    manifest.write(rd)

    carried: dict[str, dict[str, Any]] = {}
    if resume:
        carried = {b: r for b, r in SC.read_cells(run_dir(resume)).items() if r.get("answer") is not None}
        for row in carried.values():
            _append(rd, {**row, "resumed_from": resume})
    todo = [c for c in planned if c["bench_id"] not in carried]
    log(f"  arm {arm.name} ({arm.model}, {arm.effort}) on benchmark {version}: {len(planned)} open cells"
        f"{f', {len(carried)} carried from {resume}' if resume else ''}, {len(todo)} to run, "
        f"budget {'none' if budget_usd is None else f'${budget_usd:.2f}'}, run {run_id}")

    budget = manifest.budget()
    backend = cached_backend(backend_factory(arm), budget, arm.max_budget_usd_per_call, root=cache_root)
    stop = threading.Event()
    lock = threading.Lock()
    state: dict[str, Any] = {"stopped_by": None}

    def work(cell: dict[str, Any]) -> tuple[str, dict[str, Any], Any]:
        if stop.is_set():
            return "pending", cell, None
        bench_id = str(cell["bench_id"])
        try:
            with span("cell", kind="tool", bench_id=bench_id, stratum=cell.get("stratum"), arm=arm.name):
                row = V0.run_cell(backend, bench.pack(bench_id), bench.card(bench_id, drillholes=arm.switches.drillholes),
                                  bench.passages(bench_id), arm)
        except (BudgetExhausted, UsageLimitReached) as signal:
            stop.set()
            with lock:
                state["stopped_by"] = state["stopped_by"] or signal
            return "pending", cell, None
        except BackendError as err:
            return "failed", cell, err
        return "done", cell, row

    done, failed, pending = [], [], []
    with trace(run_id, KIND, run_dir=rd, arm=arm.name, bench=version):
        with ThreadPoolExecutor(max_workers=max(1, workers or arm.workers)) as pool:
            futures = [pool.submit(work, c) for c in todo]
            for fut in as_completed(futures):
                status, cell, payload = fut.result()
                base = {"cell_id": cell.get("cell_id"), "stratum": cell.get("stratum"), "fold": cell.get("fold"),
                        "arm": arm.name, "run_id": run_id,
                        "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
                if status == "done":
                    row = {**payload, **base}
                    _append(rd, row)
                    done.append(row)
                    a = row["answer"]
                    log(f"    {row['bench_id']}  {a.get('verdict', '?'):<22} p={a.get('probability', float('nan')):.2f}  "
                        f"${row['cost_usd']:.3f}  {'cache' if row['from_cache'] else 'live'}"
                        f"{'' if row['published'] else '  REJECTED: ' + row['problems'][0]}")
                elif status == "failed":
                    row = {"bench_id": cell["bench_id"], "answer": None,
                           "problems": [f"{type(payload).__name__}: {payload}"], "published": False,
                           "cost_usd": 0.0, "duration_s": None, "model_resolved": None, "cache_key": None,
                           "from_cache": False, "error": f"{type(payload).__name__}: {payload}", **base}
                    _append(rd, row)
                    failed.append(row)
                    log(f"    {cell['bench_id']}  FAILED: {payload}")
                else:
                    pending.append(str(cell["bench_id"]))

    spent = float(budget.spent_usd)  # what left the cache: the budget is charged for live calls only
    manifest.finish(spent_usd=spent)
    manifest.write(rd)
    stopped = state["stopped_by"]
    if stopped is not None:
        log(f"  stopped: {stopped}; {len(pending)} cell(s) pending, re-run with resume={run_id}")
    summary: dict[str, Any] = {
        "run_id": run_id, "run_dir": str(rd), "arm": arm.name, "model": arm.model, "effort": arm.effort,
        "version": version,
        "manifest_sha256": bench.manifest_sha256, "planned": len(planned), "carried": len(carried),
        "done": len(done), "failed": len(failed), "pending": sorted(pending), "spent_usd": round(spent, 4),
        "budget_usd": budget_usd, "budget_exhausted": isinstance(stopped, BudgetExhausted),
        "usage_limited": isinstance(stopped, UsageLimitReached),
        "resets_at_text": getattr(stopped, "resets_at_text", None), "resumed_from": resume,
        "started_at": started, "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "score": None, "mlflow_run_id": None,
    }
    if SC.read_cells(rd):
        summary["score"] = SC.score_run(rd, bench.key, boot=boot, seed=seed)
        (rd / "score.json").write_text(json.dumps(TR.jsonable(summary["score"]), indent=1) + "\n")
        s = summary["score"]
        log(f"  scored {s['n']} labelled cells: F1 {s.get('f1', float('nan')):.3f}  PR-AUC {s.get('pr_auc', float('nan')):.3f}  "
            f"abstain {s['abstain_n']}/{s['abstain_denominator']}  gate rejected {s['n_rejected']}/{s['n_answered']}  "
            f"${s['cost_usd_total']:.2f}")
    if track and summary["score"]:
        summary["mlflow_run_id"] = TR.log_run(
            f"bench.{arm.name}",
            params={"arm": arm.name, "model": arm.model, "effort": arm.effort, "agent": arm.agent,
                    "inputs": arm.as_dict()["inputs"], "switches": arm.as_dict()["switches"],
                    "prompt_version": arm.prompt_version, "bench_version": version,
                    "bench_manifest_sha256": bench.manifest_sha256, "run_id": run_id, "seed": seed,
                    "cells": len(planned)},
            metrics=_flat(summary["score"]),
            tags={"kind": KIND, "arm": arm.name, "bench": version, "run_id": run_id},
            artifacts={"summary.json": summary})
    (rd / "summary.json").write_text(json.dumps(TR.jsonable(summary), indent=1) + "\n")
    pointer = SC.out_dir(version) / "arms" / f"{arm.name}.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(TR.jsonable(summary), indent=1) + "\n")
    return summary
