"""`ue extract agent` and `ue gold`: the extractor loop, and the hand-keyed pages it is scored against.

Not registered on the main app here; `cli.py` registers `agent_cmd` as `extract agent` and mounts `gold_app`
as `gold`.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import typer

gold_app = typer.Typer(no_args_is_help=True, help="Hand-keyed gold pages, and a run's precision and recall against them (no page is keyed yet).")


def _backend(kind: str, config: Any, second_timeout_s: int) -> Any:
    """The inner backend the loop runs on; the loop wraps it in the call cache with the run budget."""
    if kind == "replay":
        from ..backends.replay import ReplayBackend

        return ReplayBackend()
    if kind == "claude":
        from ..backends.claude_cli import ClaudeCliBackend

        return ClaudeCliBackend(timeout_s=config.timeout_s, max_budget_usd=config.max_budget_usd)
    if kind == "auto":
        # API first: the second family (a vendor/model id) through OpenRouter under its output cap, a bare claude-*
        # reader through OpenRouter's Anthropic listing at the reader's own timeout; the CLI only as `--backend claude`
        from ..backends.router import api_first
        from .loop import SECOND_MAX_TOKENS

        return api_first(timeout_s=second_timeout_s, max_output_tokens=SECOND_MAX_TOKENS,
                         anthropic_timeout_s=config.timeout_s)
    raise typer.BadParameter(f"--backend must be claude, auto or replay, not {kind!r}")


def agent_cmd(
    config: str = typer.Option("phase2", "--config", help="configs/<id>.toml: the reader model, effort and page classes"),
    files: list[str] = typer.Option(None, "--files", help="file numbers (repeatable)"),
    page: list[str] = typer.Option(None, "--page", help="exact pages as <file>:<page> (repeatable)"),
    pages: int = typer.Option(None, "--pages", help="cap on planned pages"),
    max_pages_per_file: int = typer.Option(None, "--max-pages-per-file", help="override the config"),
    second_model: str = typer.Option("z-ai/glm-5.3-flash", "--second-model", help="the second family (a vendor/model id on OpenRouter)"),
    second_effort: str = typer.Option("low", "--second-effort", help="reasoning effort for the second read"),
    backend: str = typer.Option("auto", "--backend", help="auto (claude-* ids on the CLI, vendor/model ids on OpenRouter), claude, or replay"),
    budget_usd: float = typer.Option(5.0, "--budget-usd", help="ceiling on live spend for this run; a hard stop"),
    agree_only: bool = typer.Option(False, "--agree-only", help="never call the reader: only pages already read get the second family"),
    no_agree: bool = typer.Option(False, "--no-agree", help="no second family: locate, read, validate, file"),
    store: str = typer.Option(None, "--store", help="the DuckDB file the queue is filed in; default data/ue.duckdb"),
    no_store: bool = typer.Option(False, "--no-store", help="file the queue in the run directory only"),
    resume: str = typer.Option(None, "--resume", help="carry finished stages forward from this run id"),
    second_timeout_s: int = typer.Option(600, "--second-timeout-s", help="per-call timeout for the second family"),
    dry_run: bool = typer.Option(False, "--dry-run", help="print the plan and call nothing"),
) -> None:
    """Run the reading loop over routed pages: locate → read → validate → agree → file. Pages already read by
    `ue extract` are carried in as read, so the second family runs over them at no reader cost. Exits 3 on
    budget, 75 on a usage limit."""
    from ..extract import build_plan, load_config, read_results
    from .loop import (
        ExtractorRun,
        check_second_model_takes_images,
        default_store_path,
        parse_selectors,
        plan_from_results,
    )

    cfg = load_config(config)
    selectors = parse_selectors(list(page or []))
    if selectors:
        plan = plan_from_results(selectors)
        missing = [f"{f}:{p}" for f, p in selectors if not any(x.file_num == f and x.page_no == p for x in plan)]
        if missing and not agree_only:
            wanted = sorted({f for f, _ in selectors})
            routed = build_plan(cfg, files=wanted, max_pages_per_file=max_pages_per_file)
            plan += [x for x in routed if f"{x.file_num}:{x.page_no}" in missing]
            missing = [s for s in missing if not any(f"{x.file_num}:{x.page_no}" == s for x in plan)]
        if missing:
            typer.echo(f"  not planned (not read yet, or not a routed page): {', '.join(missing)}")
    else:
        plan = build_plan(cfg, files=list(files or []), pages_limit=pages, max_pages_per_file=max_pages_per_file)
    if pages is not None:
        plan = plan[:pages]
    if agree_only:
        have = read_results()
        skipped = [p for p in plan if p.page_id not in have]
        if skipped:
            typer.echo(f"  {len(skipped)} page(s) have no first-family reading and are left out (agree-only)")
        plan = [p for p in plan if p.page_id in have]
    typer.echo(f"  plan: {len(plan)} page(s) over {len({p.file_num for p in plan})} file(s); "
               f"reader {cfg.model}, second {'none' if no_agree else second_model}")
    for p in plan:
        typer.echo(f"    {p.file_num} p{p.page_no} [{p.route_class}] {p.reason}")
    if dry_run or not plan:
        return
    if backend != "replay" and not no_agree:
        check_second_model_takes_images(second_model, log=typer.echo)
    store_path = Path(store) if store else default_store_path()
    run = ExtractorRun(cfg, _backend(backend, cfg, second_timeout_s), plan, second_model=second_model,
                       second_effort=second_effort, budget_usd=budget_usd, store=None if no_store else store_path,
                       write_store=not no_store, agree_only=agree_only, skip_agree=no_agree, resume=resume,
                       log=typer.echo)
    summary = run.run()
    ag = summary["agreement"]
    typer.echo(f"  run {summary['run_id']}: {summary['done']} done, {summary['failed']} failed, "
               f"{summary['stopped']} stopped, {summary['pending']} pending; ${summary['spent_usd']:.4f} live spend"
               + (f"; agreement {ag['rate']} over {ag['n']} values on {ag['pages']} page(s), "
                  f"{summary['queue_rows']} queue row(s)" if ag.get("pages") else ""))
    if summary["usage_limited"]:
        raise typer.Exit(75)
    if summary["budget_exhausted"]:
        raise typer.Exit(3)


@gold_app.command("key")
def gold_key_cmd(
    file_num: str = typer.Argument(..., help="assessment file number"),
    page: int = typer.Argument(..., help="1-based page number of the rendered page"),
) -> None:
    """Write the empty gold skeleton for one page under gold/pages/, for a person to fill from the page image.
    Never overwrites, never prefilled from a model."""
    from .gold import write_skeleton

    try:
        write_skeleton(file_num, page, log=typer.echo)
    except (FileExistsError, FileNotFoundError) as e:
        typer.echo(f"  refused: {e}")
        raise typer.Exit(2)


@gold_app.command("score")
def gold_score_cmd(
    run: str = typer.Option(..., "--run", help="run id under data/runs"),
    which: str = typer.Option("a", "--which", help="a: the reader's readings; b: the second family's"),
    as_json: bool = typer.Option(False, "--json", help="print the score as JSON"),
) -> None:
    """Precision and recall of a run's readings against every keyed gold page it read, per field type, with
    the denominators. Reports zero gold pages when none is keyed."""
    from .gold import score_run

    out = score_run(run, which=which, log=typer.echo)
    if as_json:
        typer.echo(json.dumps({k: v for k, v in out.items() if k != "pages"}, indent=1))
