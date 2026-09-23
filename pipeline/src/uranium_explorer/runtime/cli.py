"""`ue cache` and `ue spend`: the runtime's own commands. Registered by `uranium_explorer.cli`."""

from __future__ import annotations

import typer

cache_app = typer.Typer(no_args_is_help=True, help="The call cache: re-key old records, count what is in it.")
spend_app = typer.Typer(no_args_is_help=True, help="What every backend family has spent, against the ceilings.")


@cache_app.callback()
def cache_cb() -> None:
    """The call cache: re-key old records, count what is in it."""


@spend_app.callback()
def spend_cb() -> None:
    """What every backend family has spent, against the ceilings. (A callback keeps `show` a subcommand.)"""


@cache_app.command("rekey")
def cache_rekey_cmd(
    dry_run: bool = typer.Option(False, "--dry-run", help="Count what would move and write nothing."),
) -> None:
    """Write every movable v1 extract record under the key a live request computes now (B22)."""
    from .rekey import rekey

    counts = rekey(log=typer.echo, dry_run=dry_run)
    for name in ("rekeyed", "already_rekeyed", "already_v2", "skipped_other_task", "skipped_version_mismatch",
                 "unrebuildable", "files"):
        typer.echo(f"  {name:<26}{counts.get(name, 0):>6}")


@cache_app.command("stats")
def cache_stats_cmd() -> None:
    """Records in the call cache, by task and record version."""
    from .rekey import stats

    rows = stats()
    if not rows:
        typer.echo("  the call cache is empty")
        return
    typer.echo(f"  {'task':<28}{'version':<18}{'records':>8}")
    for (task, version), n in sorted(rows.items()):
        typer.echo(f"  {task:<28}{version:<18}{n:>8}")
    typer.echo(f"  {'total':<46}{sum(rows.values()):>8}")


@spend_app.command("show")
def spend_show_cmd() -> None:
    """Spend per backend family against its ceiling, and the billed total against UE_MAX_SPEND_USD. A
    subscription family is listed at list price and bounded by each run's budget, not by a ceiling."""
    from ..backends.openai_api import load_dotenv
    from .spend import FAMILY_CAPS, SUBSCRIPTION_FAMILIES, TOTAL_CAP, billed_usd, by_family, cap_usd, ledger_path

    load_dotenv()
    per = by_family()
    families = sorted(set(per) | set(FAMILY_CAPS) | set(SUBSCRIPTION_FAMILIES))
    typer.echo(f"  {'family':<14}{'spent':>12}{'ceiling':>14}   env")
    for fam in families:
        spent = per.get(fam, 0.0)
        env = FAMILY_CAPS.get(fam, (None, None))[0]
        cap = f"${cap_usd(fam):.2f}" if env else "(run budget)" if fam in SUBSCRIPTION_FAMILIES else "(total)"
        typer.echo(f"  {fam:<14}{'$' + format(spent, '.4f'):>12}{cap:>14}   {env or ''}")
    total = billed_usd()
    typer.echo(f"  {'billed':<14}{'$' + format(total, '.4f'):>12}{'$' + format(cap_usd(), '.2f'):>14}   {TOTAL_CAP[0]}")
    typer.echo(f"  ledger  {ledger_path()}")
    if total != total:
        typer.echo("  the ledger has a line this code cannot read; every check will refuse until it is fixed")
