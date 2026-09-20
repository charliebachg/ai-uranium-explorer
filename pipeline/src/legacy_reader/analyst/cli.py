"""`lr bench-run`: run an arm over a benchmark, score a run, build the table, list the baselines.

Not registered on the main app here; `cli.py` mounts `bench_run_app` under the name it chooses.
"""

from __future__ import annotations

import json
import math
from typing import Any, Callable

import typer

bench_run_app = typer.Typer(no_args_is_help=True,
                            help="Analyst arms over a frozen benchmark, scored beside the fitted baselines.")


def _factory(kind: str) -> Callable[[Any], Any]:
    """The inner backend an arm runs on; the harness wraps it in the call cache with the run budget."""
    if kind == "replay":
        from ..backends.replay import ReplayBackend

        return lambda arm: ReplayBackend()
    if kind == "claude":
        from ..backends.claude_cli import ClaudeCliBackend

        return lambda arm: ClaudeCliBackend(timeout_s=arm.timeout_s, max_budget_usd=arm.max_budget_usd_per_call)
    raise typer.BadParameter(f"--backend must be claude or replay, not {kind!r}")


def _f(x: Any, digits: int = 3) -> str:
    return "  -  " if x is None or (isinstance(x, float) and math.isnan(x)) else f"{float(x):.{digits}f}"


def _ci(row: dict[str, Any], key: str) -> str:
    ci = row.get(f"{key}_ci")
    return f"[{_f(ci[0])},{_f(ci[1])}]" if isinstance(ci, list) and len(ci) == 2 else ""


def print_rows(rows: list[dict[str, Any]]) -> None:
    typer.echo(f"  {'row':<18} {'kind':<9} {'n':>4}  {'F1':<21} {'PR-AUC':<21} {'ROC':<6} {'ECE':<6} {'abstain':<8} {'cost':>7}")
    for r in rows:
        typer.echo(f"  {r['name']:<18} {r['kind']:<9} {r.get('n_cells', 0):>4}  "
                   f"{_f(r.get('f1'))} {_ci(r, 'f1'):<15} {_f(r.get('pr_auc'))} {_ci(r, 'pr_auc'):<15} "
                   f"{_f(r.get('roc_auc'))}  {_f(r.get('ece'))}  {_f(r.get('abstain_rate')):<8} "
                   f"${float(r.get('cost_usd') or 0):.2f}")


@bench_run_app.command("run")
def run_cmd(
    version: str = typer.Option("v1", "--version", help="benchmark version under data/bench/"),
    arm: str = typer.Option("v0", "--arm", help="arm name under configs/arms/"),
    budget_usd: float = typer.Option(80.0, "--budget-usd", help="ceiling on live spend for this run"),
    workers: int = typer.Option(None, "--workers", help="parallel cells; default from the arm"),
    cells: list[str] = typer.Option(None, "--cells", help="bench ids to run (repeatable or comma-separated); "
                                                          "default every open cell"),
    track: bool = typer.Option(True, "--track/--no-track", help="log one MLflow run for the arm"),
    backend: str = typer.Option("claude", "--backend", help="claude (live, cached) or replay (recorded only)"),
    resume: str = typer.Option(None, "--resume", help="carry finished cells forward from this run id"),
) -> None:
    """Run one arm over the open cells of a frozen benchmark. Exits 3 on budget, 75 on a usage limit."""
    from .arms import load_arm
    from .run import run_arm

    ids = [x.strip() for c in (cells or []) for x in c.split(",") if x.strip()]
    try:
        summary = run_arm(version, load_arm(arm), _factory(backend), budget_usd, typer.echo, cells=ids or None,
                          workers=workers, track=track, resume=resume)
    except PermissionError as e:
        typer.echo(f"refused: {e}")
        raise typer.Exit(2) from e
    typer.echo(f"  run {summary['run_id']}: {summary['done']} done, {summary['failed']} failed, "
               f"{len(summary['pending'])} pending, ${summary['spent_usd']:.2f} live spend"
               + (f", mlflow {summary['mlflow_run_id']}" if summary.get("mlflow_run_id") else ""))
    if summary["usage_limited"]:
        raise typer.Exit(75)
    if summary["budget_exhausted"]:
        raise typer.Exit(3)


@bench_run_app.command("score")
def score_cmd(
    version: str = typer.Option("v1", "--version"),
    run: str = typer.Option(..., "--run", help="run id to score against the key"),
) -> None:
    """Score one run's cells.jsonl against the benchmark key and write score.json beside it."""
    from ..prospect import tracking as TR
    from .frozen import load_bench
    from .run import run_dir
    from .score import score_run

    if run_dir is None:
        raise typer.BadParameter("the runtime package is not available")
    rd = run_dir(run)
    try:
        score = score_run(rd, load_bench(version).key)
    except PermissionError as e:
        typer.echo(f"refused: {e}")
        raise typer.Exit(2) from e
    (rd / "score.json").write_text(json.dumps(TR.jsonable(score), indent=1) + "\n")
    for k in ("n", "n_pos", "precision", "recall", "f1", "pr_auc", "roc_auc", "pr_auc_all", "roc_auc_all", "ece",
              "abstain_rate", "gate_rejection_rate", "probe_abstain_rate", "cost_usd_per_cell", "latency_s_per_cell"):
        ci = score.get(f"{k}_ci")
        typer.echo(f"  {k:<22} {_f(score.get(k))}" + (f"  [{_f(ci[0])}, {_f(ci[1])}]" if ci else ""))
    typer.echo(f"  wrote {rd / 'score.json'}")


@bench_run_app.command("regate")
def regate_cmd(
    version: str = typer.Option("v1", "--version"),
    run: list[str] = typer.Option(..., "--run", help="run id(s) to re-gate and re-score (repeatable)"),
) -> None:
    """Re-apply the current gate to a run's stored answers and re-score it. No model calls."""
    from .run import regate_run

    for rid in run:
        regate_run(version, rid, log=typer.echo)


@bench_run_app.command("table")
def table_cmd(version: str = typer.Option("v1", "--version")) -> None:
    """Merge every arm's latest run with the baselines into data/out/bench/<version>/table.json."""
    from .score import out_dir, table

    out = table(version, log=typer.echo)
    print_rows(out["rows"])
    typer.echo(f"  wrote {out_dir(version) / 'table.json'} and derived.metric bench.{version}.*")


@bench_run_app.command("baselines")
def baselines_cmd(version: str = typer.Option("v1", "--version")) -> None:
    """The baseline rows alone: chance, copy-the-learned-score, and the three fitted scores."""
    from ..store import connect
    from .score import baselines

    con = connect(read_only=True)
    try:
        rows = baselines(version, con, log=typer.echo)
    finally:
        con.close()
    print_rows(rows)
