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

The runtime pieces are `uranium_explorer.runtime`'s: the manifest and its budget, the run directory, the cached
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
from ..prospect.criteria import load as load_criteria
from . import families as FAM
from . import frozen as F
from . import loop as LOOP
from . import prompts as PR
from . import score as SC
from . import v0 as V0
from . import wire as W
from .arms import ArmConfig, load_arm
from .session import Session

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


#: how a v1 run opens the session for one cell; a test hands in a factory over a fake tool world
SessionFactory = Callable[[Any, dict[str, Any], ArmConfig, Path, dict[str, Any]], Any]


def open_session(bench: Any, cell: dict[str, Any], arm: ArmConfig, stage: Path, shared: dict[str, Any]) -> Any:
    """A benchmark session for one cell: the real cell id from the benchmark's cells file, the bench id the
    model sees, the frozen blind list, the cell's fold for the out-of-fold scores, and the arm's switches.
    `shared` carries what a run computes once for every cell (the forbidden-name set)."""
    bench_id = str(cell["bench_id"])
    return Session.open(str(cell["cell_id"]), "benchmark", fold=cell.get("fold"), switches=arm.switches,
                        bench_id=bench_id, blind=[str(f) for f in bench.blind(bench_id)], stage=stage,
                        forbidden=shared.get("forbidden"))


def _forbidden_names() -> set[str] | None:
    """The name set a benchmark session scrubs with, read from the store once per run rather than once per
    cell. None when there is no store to read (a test world), and the session then reads it lazily."""
    from ..bench import pack as P
    from ..store import connect

    try:
        con = connect(read_only=True)
    except Exception:  # noqa: BLE001 - no store: the session decides
        return None
    try:
        return P.forbidden_strings(con) | P.hole_names(con)
    finally:
        con.close()


def _preload_layers(log: Callable[[str], None]) -> int:
    """Read every map layer the template plan's `nearby` and `crosscheck` calls touch, once, on the main
    thread, before any worker starts. The loader is cached per process, but forty workers making their first
    call at the same moment each parsed the same 24,000-feature layer before the cache held it, and the
    runner reached 6.9 GB on a 9 GB machine. Layers that cannot be read (a test world) are skipped and said."""
    from ..prospect import tools as T
    from .plan import FEATURE_LAYER

    layers = sorted(set(FEATURE_LAYER.values()) | {"em_conductors", "faults_250k"})
    loaded = 0
    for layer in layers:
        try:
            T.layer_features(layer)
            loaded += 1
        except Exception as err:  # noqa: BLE001 - no pulled layer here: the tools will say so per call
            log(f"  layer {layer} not preloaded: {type(err).__name__}")
    return loaded


def _store_sha() -> str | None:
    """The store's content hash, so a v1 run (which reads the live tools, not the frozen packs) names the
    store it ran against; None when the snapshot module cannot say."""
    try:
        from ..store.snapshot import store_sha

        return store_sha()
    except Exception:  # noqa: BLE001
        return None


def open_dashboard_session(cell_id: str, arm: ArmConfig, stage: Path, shared: dict[str, Any]) -> Any:
    """A dashboard session over a real cell: nothing blinded, the label kept, the store's own connection when
    the run holds one (a chain run writes to the store, and DuckDB will not open the same file read-only
    beside that)."""
    return Session.open(cell_id, "dashboard", fold=None, switches=arm.switches, stage=stage,
                        con=shared.get("con"))


def render_cards(cell_ids: list[str], out_dir: Path, spec_version: str = "v1",
                 log: Callable[[str], None] = print, con: Any = None) -> dict[str, Path]:
    """The map card of each real cell, drawn exactly as the benchmark builder draws them (same spec, same
    layers, same window), so a chain over a dashboard cell reads the picture a benchmark cell would."""
    from shapely import wkb

    from ..bench.card import card_layers, png_bytes, render_card
    from ..bench.spec import load_spec
    from ..store import connect

    spec = load_spec(spec_version)
    layers = card_layers(spec)
    own = con is None
    con = con or connect(read_only=True)
    try:
        marks = ",".join("?" * len(cell_ids))
        geoms = {cid: wkb.loads(bytes(g)) for cid, g in con.execute(
            f"select cell_id, geom_wkb from derived.cell where cell_id in ({marks})", list(cell_ids)).fetchall()}
    finally:
        if own:
            con.close()
    out: dict[str, Path] = {}
    out_dir.mkdir(parents=True, exist_ok=True)
    for cid in cell_ids:
        if cid not in geoms:
            log(f"  no such cell {cid}: no card")
            continue
        path = out_dir / f"{cid}.png"
        path.write_bytes(png_bytes(render_card(cid, spec, layers, geoms[cid], drillholes=spec.card.drillholes)))
        out[cid] = path
    return out


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


def regate_run(version: str, run_id: str, log: Callable[[str], None] = print, boot: int = 1000,
               seed: int = 0) -> dict[str, Any]:
    """Re-apply the gate to a finished run's stored answers and re-score it, without any model call.

    The gate is deterministic code; when it changes (a number glued to its unit was not recognised as the
    quoted number it is), every arm is re-judged by the same rule from the answers on disk, so the arms stay
    comparable. The first version of each row is kept beside it in `cells.pre-regate.jsonl`."""
    bench = F.load_bench(version)
    rd = run_dir(run_id)
    rows = SC.read_cells(rd)
    if not rows:
        raise FileNotFoundError(f"no cells in {rd}")
    backup = rd / "cells.pre-regate.jsonl"
    if not backup.is_file():
        backup.write_bytes((rd / "cells.jsonl").read_bytes())
    changed = 0
    out_rows = []
    for bench_id, row in rows.items():
        answer = row.get("answer")
        if isinstance(answer, dict) and not row.get("chain"):  # a v1 chain is re-gated by re-running its loop
            arm = load_arm(str(row["arm"]))
            shown = V0.apply_switches(bench.pack(bench_id), arm.switches)
            passages = bench.passages(bench_id, arm.passages_view) if arm.inputs.passages else None
            problems = V0.gate(answer, shown, context=V0.gate_context(shown, passages))
            if problems != (row.get("problems") or []) or bool(row.get("published")) != (not problems):
                changed += 1
            row = {**row, "problems": problems, "published": not problems}
        out_rows.append(row)
    (rd / "cells.jsonl").write_text("".join(json.dumps(TR.jsonable(x)) + "\n" for x in out_rows))
    score = SC.score_run(rd, bench.key, boot=boot, seed=seed)
    (rd / "score.json").write_text(json.dumps(TR.jsonable(score), indent=1) + "\n")
    summary_path = rd / "summary.json"
    summary = json.loads(summary_path.read_text()) if summary_path.is_file() else {"run_id": run_id, "run_dir": str(rd)}
    summary["score"] = score
    summary["regated_at"] = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    summary["regate_changed"] = changed
    summary_path.write_text(json.dumps(TR.jsonable(summary), indent=1) + "\n")
    arm_name = summary.get("arm") or next((str(x["arm"]) for x in out_rows if x.get("arm")), None)
    if arm_name:
        pointer = SC.out_dir(str(summary.get("version") or version)) / "arms" / f"{arm_name}.json"
        pointer.parent.mkdir(parents=True, exist_ok=True)
        pointer.write_text(json.dumps(TR.jsonable(summary), indent=1) + "\n")
    log(f"  re-gated {run_id}: {changed} row(s) changed; F1 {score.get('f1', float('nan')):.3f}  "
        f"PR-AUC {score.get('pr_auc', float('nan')):.3f}  gate rejected {score['n_rejected']}/{score['n_answered']}")
    return summary


def _run_staged(backend: Any, bench: F.Bench, cell: dict[str, Any], arm: ArmConfig, cfg: Any, criteria: Any,
                rd: Path, run_id: str, shared: dict[str, Any], session_factory: SessionFactory = open_session,
                ) -> dict[str, Any]:
    """One cell through the staged loop: a session opened for it, the loop run, the row keyed by the bench
    id whatever the session called the cell, and the session's own manifest fields kept on the row so the
    run's manifest can say which scores and how many refusals every cell saw."""
    bench_id = str(cell["bench_id"])
    stage = rd / "stages" / bench_id
    session = session_factory(bench, cell, arm, stage, shared)
    try:
        row = LOOP.run_cell_v1(backend, session, bench.card(bench_id, drillholes=arm.switches.drillholes),
                               arm.inputs, arm.switches, cfg, criteria, chain_id=f"{run_id}:{bench_id}",
                               run_id=run_id, stage=stage, manifest_sha256=bench.manifest_sha256, arm=arm.name)
        row["bench_id"] = bench_id
        row["session"] = session.manifest_fields()
    finally:
        session.close()
    return row


def run_arm(
    version: str, arm: ArmConfig | str, backend_factory: Factory, budget_usd: float | None,
    log: Callable[[str], None] = print, cells: list[str] | None = None, workers: int | None = None,
    track: bool = True, resume: str | None = None, seed: int = 0, boot: int = SC.BOOT,
    cache_root: Path | None = None, session_factory: SessionFactory = open_session, sample: int = 0,
) -> dict[str, Any]:
    """One arm over the benchmark's open cells (or the named open cells). Returns the run summary, which is
    also written to `<run_dir>/summary.json` and `data/out/bench/<version>/arms/<arm>.json`.

    A v0 arm answers from the frozen pack and card in one call; a v1 arm runs the staged loop over a session
    that serves the live tools under the benchmark's blind list, fold and switches (`session_factory`), so
    the manifest also names the store the tools read from."""
    arm = load_arm(arm) if isinstance(arm, str) else arm
    bench = F.load_bench(version)
    staged = arm.agent == "v1"
    if staged and arm.loop is None:
        raise ValueError(f"arm {arm.name} is a v1 arm without an [arm.loop] table")
    cfg = arm.loop.config(arm.effort, arm.prompt_version) if staged else None
    criteria = load_criteria() if staged else None
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

    config = arm.as_dict()
    if staged:
        config["store_sha"] = _store_sha()
    manifest = Manifest.start(kind=KIND, config=config, models=cfg.models() if staged else {"analyst": arm.model},
                              seed=seed, budget_usd=budget_usd,
                              bench={"bench_id": version, "manifest_sha256": bench.manifest_sha256})
    # the directory is claimed before anything is written: two arms launched in the same second used to be
    # given one run id and interleaved their rows; the second now gets a -2 suffix on its id and directory
    manifest.run_id, rd = _claim(str(manifest.run_id))
    if staged:
        _note(manifest, "prompt_hashes", PR.prompt_hashes())
        _note(manifest, "schema_hashes", {
            f"node/{W.SCHEMA_VERSION}": sha256_json(W.NODE_SCHEMA),
            f"verifier/{W.SCHEMA_VERSION}": sha256_json(W.VERIFIER_SCHEMA),
            f"adjudicator/{W.SCHEMA_VERSION}": sha256_json(W.ADJUDICATOR_SCHEMA),
            f"plan/{W.SCHEMA_VERSION}": sha256_json(W.PLAN_SCHEMA),
            f"nodes/{W.SCHEMA_VERSION}": sha256_json(W.NODES_SCHEMA),
        })
    elif arm.agent == "v2":
        _note(manifest, "prompt_hashes", {f"{arm.prompt_version}/{k}": v for k, v in FAM.prompt_hashes(arm.switches).items()})
        _note(manifest, "schema_hashes", FAM.schema_hashes())
    else:
        system = V0.system_for(arm.switches)
        _note(manifest, "prompt_hashes", {arm.prompt_version: sha256_json(system)})
        _note(manifest, "schema_hashes", {V0.SCHEMA_VERSION: sha256_json(V0.ANSWER_SCHEMA)})
        if arm.switches.oof_scores:
            _extend(manifest, "scores_seen", [{"model_version": m, "fold_kind": SC.FOLD_KIND} for m in SC.BASELINE_MODELS])
    run_id = str(manifest.run_id)
    manifest.write(rd)
    shared: dict[str, Any] = {"forbidden": _forbidden_names()} if staged else {}
    if staged:
        log(f"  preloaded {_preload_layers(log)} map layers for the workers")

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
                card = bench.card(bench_id, drillholes=arm.switches.drillholes)
                if staged:
                    row = _run_staged(backend, bench, cell, arm, cfg, criteria, rd, run_id, shared,
                                      session_factory=session_factory)
                elif arm.agent == "v2":
                    row = FAM.run_cell(backend, bench.pack(bench_id), card, arm, sample=sample,
                                       passages=bench.passages(bench_id, arm.passages_view) if arm.inputs.passages
                                       else None)
                else:
                    row = V0.run_cell(backend, bench.pack(bench_id), card,
                                      bench.passages(bench_id, arm.passages_view), arm, sample=sample)
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
                    st = row.get("stages") or {}
                    stage_note = (f"  rounds {st.get('rounds')} {'valid' if st.get('valid') else 'never valid'}"
                                  f"  gate {st.get('n_gate_rejections')}/{st.get('attempts_total')}" if st else "")
                    log(f"    {row['bench_id']}  {a.get('verdict', '?'):<22} p={a.get('probability', float('nan')):.2f}  "
                        f"${row['cost_usd']:.3f}  {'cache' if row['from_cache'] else 'live'}{stage_note}"
                        f"{'' if row['published'] else '  REJECTED: ' + (row['problems'] or ['?'])[0]}")
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
    if staged:
        seen: dict[str, dict[str, Any]] = {}
        for row in done:
            for entry in (row.get("session") or {}).get("scores_seen") or []:
                seen.setdefault(json.dumps(entry, sort_keys=True), entry)
        _extend(manifest, "scores_seen", list(seen.values()))
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
        "score": None, "mlflow_run_id": None, "sample": int(sample),
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
    pointer = SC.out_dir(version) / "arms" / f"{SC.sample_name(arm.name, sample)}.json"
    pointer.parent.mkdir(parents=True, exist_ok=True)
    pointer.write_text(json.dumps(TR.jsonable(summary), indent=1) + "\n")
    return summary


# ---------------------------------------------------------------- real cells, for the dashboard


def _why_withheld(row: dict[str, Any]) -> str:
    """The first reason a chain was not published: a gate or store problem, else the abstention (a chain no
    round validated), else the verifier's last word."""
    if row.get("problems"):
        return str(row["problems"][0])
    decision = (row.get("chain") or {}).get("decision") or {}
    if decision.get("abstained_reason"):
        return str(decision["abstained_reason"])
    verdicts = (row.get("chain") or {}).get("verdicts") or []
    if verdicts and not verdicts[-1].get("valid"):
        faulty = ", ".join(f.get("node_id", "?") for f in verdicts[-1].get("faulty") or [])
        return f"never valid; faulty {faulty or 'none named'}: {str(verdicts[-1].get('feedback') or '')[:90]}"
    unknown = [n for n in (row.get("chain") or {}).get("nodes") or [] if not n.get("published")]
    if unknown:
        last = unknown[-1]
        return (f"{len(unknown)} node(s) recorded unknown after three gate attempts; last {last.get('criterion')}: "
                f"{str((last.get('problems') or ['?'])[0])[:90]}")
    return "not published"

CHAIN_KIND = "chain"


def run_cells(
    cell_ids: list[str], arm: ArmConfig | str, backend_factory: Factory, budget_usd: float | None,
    log: Callable[[str], None] = print, workers: int = 1, track: bool = True, cache_root: Path | None = None,
    session_factory: Callable[[str, ArmConfig, Path, dict[str, Any]], Any] = open_dashboard_session,
    cards: Callable[..., dict[str, Path]] = render_cards, con: Any = None, seed: int = 0,
) -> dict[str, Any]:
    """The staged loop over real cells for the dashboard: nothing blinded, the chains stored in
    the agent tier as they publish, the rows and the per-stage metrics beside them in the run directory.

    The store is written to, so the run holds one read-write connection and hands each worker a cursor of
    it; the process must be in one-connection mode (`UE_STORE_RW=1`, as `ue prospect serve` runs) because
    the tools open their own read-only handles and DuckDB refuses both kinds on one file."""
    from ..store import connect

    arm = load_arm(arm) if isinstance(arm, str) else arm
    if arm.agent != "v1" or arm.loop is None:
        raise ValueError(f"arm {arm.name} is not a v1 arm; only the staged loop stores chains")
    cfg = arm.loop.config(arm.effort, arm.prompt_version)
    criteria = load_criteria()
    started = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
    manifest = Manifest.start(kind=CHAIN_KIND, config={**arm.as_dict(), "store_sha": _store_sha(), "cells": list(cell_ids)},
                              models=cfg.models(), seed=seed, budget_usd=budget_usd, bench=None)
    manifest.run_id, rd = _claim(str(manifest.run_id))
    _note(manifest, "prompt_hashes", PR.prompt_hashes())
    _note(manifest, "schema_hashes", {f"node/{W.SCHEMA_VERSION}": sha256_json(W.NODE_SCHEMA),
                                      f"verifier/{W.SCHEMA_VERSION}": sha256_json(W.VERIFIER_SCHEMA),
                                      f"adjudicator/{W.SCHEMA_VERSION}": sha256_json(W.ADJUDICATOR_SCHEMA),
                                      f"plan/{W.SCHEMA_VERSION}": sha256_json(W.PLAN_SCHEMA),
                                      f"nodes/{W.SCHEMA_VERSION}": sha256_json(W.NODES_SCHEMA)})
    run_id = str(manifest.run_id)
    manifest.write(rd)
    own_con = con is None
    con = con or connect()
    try:
        card_paths = cards(list(cell_ids), rd / "cards", log=log, con=con) if arm.inputs.card else {}
        log(f"  preloaded {_preload_layers(log)} map layers for the workers")
        log(f"  chains: arm {arm.name} over {len(cell_ids)} cell(s), budget "
            f"{'none' if budget_usd is None else f'${budget_usd:.2f}'}, run {run_id}")
        budget = manifest.budget()
        backend = cached_backend(backend_factory(arm), budget, arm.max_budget_usd_per_call, root=cache_root)
        stop = threading.Event()
        lock = threading.Lock()
        state: dict[str, Any] = {"stopped_by": None}

        def work(cell_id: str) -> tuple[str, str, Any]:
            if stop.is_set():
                return "pending", cell_id, None
            cursor = con.cursor()
            try:
                stage = rd / "stages" / cell_id
                session = session_factory(cell_id, arm, stage, {"con": cursor})
                try:
                    with span("cell", kind="tool", cell_id=cell_id, arm=arm.name):
                        row = LOOP.run_cell_v1(backend, session, card_paths.get(cell_id), arm.inputs, arm.switches,
                                               cfg, criteria, chain_id=f"{run_id}:{cell_id}", run_id=run_id,
                                               con=cursor, stage=stage, arm=arm.name)
                    row["bench_id"] = cell_id   # the rows are keyed by the cell asked for, as a benchmark's are
                    row["session"] = session.manifest_fields()
                finally:
                    session.close()
            except (BudgetExhausted, UsageLimitReached) as signal:
                stop.set()
                with lock:
                    state["stopped_by"] = state["stopped_by"] or signal
                return "pending", cell_id, None
            except BackendError as err:
                return "failed", cell_id, err
            finally:
                cursor.close()
            return "done", cell_id, row

        done, failed, pending = [], [], []
        with trace(run_id, CHAIN_KIND, run_dir=rd, arm=arm.name):
            with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
                for fut in as_completed([pool.submit(work, c) for c in cell_ids]):
                    status, cell_id, payload = fut.result()
                    base = {"cell_id": cell_id, "arm": arm.name, "run_id": run_id,
                            "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds")}
                    if status == "done":
                        row = {**payload, **base}
                        _append(rd, row)
                        done.append(row)
                        st = row.get("stages") or {}
                        log(f"    {cell_id}  {row['answer'].get('verdict', '?'):<22} p={row['answer'].get('probability', float('nan')):.2f}"
                            f"  ${row['cost_usd']:.3f}  rounds {st.get('rounds')} {'valid' if st.get('valid') else 'never valid'}"
                            f"  gate {st.get('n_gate_rejections')}/{st.get('attempts_total')}"
                            f"{'' if row['published'] else '  WITHHELD: ' + _why_withheld(row)}")
                    elif status == "failed":
                        row = {"bench_id": cell_id, "answer": None, "problems": [f"{type(payload).__name__}: {payload}"],
                               "published": False, "cost_usd": 0.0, "duration_s": None, "error": str(payload), **base}
                        _append(rd, row)
                        failed.append(row)
                        log(f"    {cell_id}  FAILED: {payload}")
                    else:
                        pending.append(cell_id)
    finally:
        if own_con:
            con.close()
    spent = float(budget.spent_usd)
    manifest.finish(spent_usd=spent)
    manifest.write(rd)
    stopped = state["stopped_by"]
    if stopped is not None:
        log(f"  stopped: {stopped}; {len(pending)} cell(s) pending")
    rows = SC.read_cells(rd)
    stages = SC.stage_metrics(rows)
    summary: dict[str, Any] = {
        "run_id": run_id, "run_dir": str(rd), "kind": CHAIN_KIND, "arm": arm.name, "models": cfg.models(),
        "cells": list(cell_ids), "done": len(done), "failed": len(failed), "pending": sorted(pending),
        "published": sum(1 for r in done if r.get("published")), "spent_usd": round(spent, 4), "budget_usd": budget_usd,
        "budget_exhausted": isinstance(stopped, BudgetExhausted), "usage_limited": isinstance(stopped, UsageLimitReached),
        "resets_at_text": getattr(stopped, "resets_at_text", None), "started_at": started,
        "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"), "stages": stages, "mlflow_run_id": None,
    }
    if track and stages:
        summary["mlflow_run_id"] = TR.log_run(
            f"chain.{arm.name}",
            params={"arm": arm.name, "agent": arm.agent, "models": cfg.models(), "effort": arm.effort,
                    "prompt_version": arm.prompt_version, "run_id": run_id, "cells": len(cell_ids)},
            metrics=_flat(stages), tags={"kind": CHAIN_KIND, "arm": arm.name, "run_id": run_id},
            artifacts={"summary.json": summary})
    (rd / "summary.json").write_text(json.dumps(TR.jsonable(summary), indent=1) + "\n")
    return summary
