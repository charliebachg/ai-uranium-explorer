"""`lr bench`: build, audit and show a benchmark version; compute the out-of-fold score table.

Registered on the main CLI as `app.add_typer(bench_app, name="bench")`. Nothing here computes: each command
calls one function of the package and prints what it returns.
"""

from __future__ import annotations

import typer

bench_app = typer.Typer(no_args_is_help=True, help="UraniumBench: frozen, anonymised benchmark cells.")


@bench_app.command("build")
def build_cmd(version: str = typer.Option("v1", "--version", help="spec under configs/bench/<version>.toml")) -> None:
    """Write data/bench/<version>/ from the spec and the store (resumable; the store is read only)."""
    from .build import build

    m = build(version, log=typer.echo)
    typer.echo(f"built {version}: {m['counts']['cells']} cells, {len(m['files'])} files hashed")


@bench_app.command("audit")
def audit_cmd(version: str = typer.Option("v1", "--version")) -> None:
    """Re-hash every file and scan every pack and passage for anything that places or names the ground."""
    from .build import audit

    problems = audit(version)
    for p in problems:
        typer.echo(f"  {p}")
    typer.echo(f"{version}: {'clean' if not problems else f'{len(problems)} problem(s)'}")
    raise typer.Exit(1 if problems else 0)


@bench_app.command("show")
def show_cmd(version: str = typer.Option("v1", "--version")) -> None:
    """Counts per stratum and split, shortfalls, and what was hashed."""
    from .build import show

    show(version, log=typer.echo)


@bench_app.command("oof-scores")
def oof_scores_cmd(seed: int = typer.Option(0, "--seed"),
                   write: bool = typer.Option(False, "--write/--no-write",
                                              help="write derived.cell_score_oof (otherwise print a summary)")) -> None:
    """Out-of-fold learned, effort and criteria scores for every scorable cell under 30 km spatial folds."""
    from ..store import connect
    from .oof import oof_scores, write_oof_scores

    df = oof_scores(seed)
    for model, g in df.groupby("model"):
        ok = g["score"].notna()
        typer.echo(f"  {model:9} {int(ok.sum()):,} scored of {len(g):,}  mean {g['score'].mean():.3f}")
    if write:
        con = connect()
        try:
            n = write_oof_scores(df, con, run_id=f"bench-oof-seed{seed}")
        finally:
            con.close()
        typer.echo(f"wrote {n:,} rows to derived.cell_score_oof")
    else:
        typer.echo("not written (pass --write)")
