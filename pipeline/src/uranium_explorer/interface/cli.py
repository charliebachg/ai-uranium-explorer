"""`ue interface ask`: one conversation with the interface agent from the terminal, under a hard budget.

Built for a cheap smoke test, and for anyone who wants to see a turn routed without the web
app: the store is read read-only, an insight is written only to the store you name with `--insight-store`
(a temporary copy, never the live one), and `--budget-usd` is a ceiling the call cache refuses to cross.
Every turn prints its route, whether the gate passed, and what it cost; the ledger's own figure for the run
is printed at the end, beside the sum the turns reported.
"""

from __future__ import annotations

import json
from functools import partial
from pathlib import Path
from typing import Any

import typer

interface_app = typer.Typer(no_args_is_help=True,
                            help="The interface agent: route, plan, answer, gate, from the terminal.")

#: the worst case one interface call is allowed to cost, checked against the run budget before it is sent
ESTIMATE_USD = 0.05


def _backend(kind: str, budget_usd: float | None, refresh: bool) -> Any:
    from ..backends.cache import CachedBackend
    from ..runtime.spend import RunBudget

    if kind == "openai":
        from ..backends.openai_api import OpenAIBackend

        inner: Any = OpenAIBackend()
    elif kind == "claude":
        from ..backends.claude_cli import ClaudeCliBackend

        inner = ClaudeCliBackend(timeout_s=600, max_budget_usd=1.20)
    elif kind == "auto":
        from ..backends.claude_cli import ClaudeCliBackend
        from ..backends.openrouter import OpenRouterBackend, is_openrouter_model
        from ..backends.router import RoutedBackend

        inner = RoutedBackend([(is_openrouter_model, OpenRouterBackend(timeout_s=180))],
                              default=ClaudeCliBackend(timeout_s=600, max_budget_usd=1.20))
    else:
        raise typer.BadParameter(f"--backend must be openai, claude or auto, not {kind!r}")
    return CachedBackend(inner, refresh=refresh, run_budget=RunBudget(cap_usd=budget_usd), estimate_usd=ESTIMATE_USD)


@interface_app.command("ask")
def ask_cmd(
    cell: str = typer.Option(..., "--cell", help="the cell the conversation is about, like 0201_0072"),
    questions: list[str] = typer.Option(..., "--question", "-q", help="one turn each, in order"),
    backend: str = typer.Option("auto", "--backend", help="auto | openai | claude"),
    model: str = typer.Option("", "--model", help="default: UE_INTERFACE_MODEL, else z-ai/glm-5.3-flash"),
    effort: str = typer.Option("low", "--effort"),
    budget_usd: float = typer.Option(0.50, "--budget-usd", help="a hard stop on live spend for this run"),
    insight_store: str = typer.Option("", "--insight-store", help="a DuckDB file record_insight may write to; "
                                                                  "without one an insight is refused"),
    requested_by: str = typer.Option("local", "--requested-by", help="the author label an insight is recorded under"),
    refresh: bool = typer.Option(False, "--refresh", help="ignore cached model replies"),
    as_json: bool = typer.Option(False, "--json", help="every turn as one JSON line"),
) -> None:
    """Ask the interface agent about one cell, one or more turns in one conversation."""
    from ..runtime.spend import spent_usd
    from ..store import connect
    from . import default_model
    from .agent import ask
    from .conversation import Conversation

    model = model or default_model()
    store = partial(connect, Path(insight_store), False) if insight_store else None
    conv = Conversation(cell_id=cell, requested_by=requested_by, insight_store=store)
    chosen = _backend(backend, budget_usd, refresh)
    before = spent_usd()
    typer.echo(f"  cell {cell}, model {model}, budget ${budget_usd:.2f}, insights to "
               f"{insight_store or 'nowhere (refused)'}")
    total = 0.0
    for i, q in enumerate(questions, start=1):
        events: list[dict[str, Any]] = []

        def on_event(e: dict[str, Any]) -> None:
            events.append(e)
            if not as_json and e.get("type") in ("route", "tool", "tool_error", "abstain", "insight", "job", "refused"):
                short = {k: v for k, v in e.items() if k in ("kind", "topic", "out_of_scope", "tool", "args", "reason",
                                                               "expert_id", "job_id", "error", "problems", "reused")}
                typer.echo(f"    {e['type']}: {json.dumps(short, default=str)}")

        try:
            turn = ask(conv, q, chosen, model=model, effort=effort, on_event=on_event)
        except Exception as err:  # noqa: BLE001 - the budget or the provider stopped the run: say so and keep the turns done
            typer.echo(f"  turn {i} stopped: {type(err).__name__}: {err}")
            break
        total += float(turn.get("cost_usd") or 0.0)
        if as_json:
            typer.echo(json.dumps({"turn": i, "question": q, **turn}, default=str))
            continue
        route = turn.get("route") or {}
        gate = "passed" if turn.get("published") else "refused"
        typer.echo(f"  turn {i}: {q}")
        typer.echo(f"    route {route.get('kind')} (topic {route.get('topic')}, out_of_scope {route.get('out_of_scope')}), "
                   f"gate {gate}{' after a retry' if turn.get('retried') else ''}, ${turn.get('cost_usd', 0):.4f}")
        if turn.get("text"):
            typer.echo(f"    {turn['text']}")
        for p in turn.get("problems") or []:
            typer.echo(f"    objection: {p}")
        for c in turn.get("claims") or []:
            typer.echo(f"    claim: {c.get('text')}  {c.get('value_ids')}")
    after = spent_usd()
    typer.echo(f"  turns reported ${total:.4f}; the ledger moved ${after - before:.4f} (cache hits cost nothing)")


__all__ = ["interface_app"]
