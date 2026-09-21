"""`ue serve`: a small local service so the web app can ask the agent about a cell.

The published site is static files, which is right for everything precomputed: scores, features, memos. A
conversation is not precomputed, so it needs something that can call the model. This is that, and nothing more
— it runs on localhost, it is not an authenticated public API, and the public-safe build simply does not offer
the chat.

Every endpoint reads the same evidence record the map and the panels are drawn from, so the three views cannot
disagree with each other. No endpoint invents a number: `/api/chat` answers are passed through the same gate
that guards a published memo.
"""

from __future__ import annotations

import json
from typing import Any, Callable

import duckdb

from ..analyst import chains as C
from ..store import connect
from ..values import stat
from . import tools as T

DEFAULT_PORT = 8787

#: how many of a cell's chains the record carries: the panel opens the newest and lists the rest to pick from
CHAIN_LIMIT = 5


def chain_value_id(chain_id: str, *parts: str) -> str:
    """The id a chain's own number is registered under: `c:chain:<chain_id>:<field>`, with a node's id before
    the field for a strength. The panel builds the same ids from the chain it is drawing, as it does for a
    criterion's weight, so the chain's JSON carries the numbers as the store holds them and nothing else."""
    return ":".join(("c:chain", chain_id, *parts))


def _chain_record(loaded: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
    """One chain as the record serves it, with the value records its numbers print through.

    Only the current attempt of each node is served: the earlier attempts are the store's history of the repair,
    and the chain the verifier judged and the adjudicator read is the one that stands. The numbers a chain holds
    as plain columns (a probability, the weighted score, a node's strength) are registered as stats here, because
    the panel may not print a bare number; a null column registers nothing, and the panel shows it as absent.
    """
    chain, decision = loaded["chain"], loaded["decision"]
    chain_id = str(chain["chain_id"])
    values: dict[str, Any] = {}

    def mint(vid: str, value: Any, fmt: str, note: str, unit: str | None = None) -> None:
        if isinstance(value, (int, float)) and not isinstance(value, bool):
            values[vid] = stat(vid, value, fmt=fmt, note=f"{note}, from chain {chain_id}", unit=unit)

    mint(chain_value_id(chain_id, "final_probability"), chain.get("final_probability"), "ratio3",
         "the probability the chain publishes with its verdict")
    mint(chain_value_id(chain_id, "weighted_score"), chain.get("weighted_score"), "ratio3",
         "decider (a): the weighted sum over node strengths, weights fitted out-of-fold")
    mint(chain_value_id(chain_id, "rounds"), chain.get("rounds"), "int", "verifier rounds run")
    mint(chain_value_id(chain_id, "cost_usd"), chain.get("cost_usd"), "m2", "list-price cost of the chain", "USD")

    nodes = []
    for n in C.current_nodes(loaded["nodes"]):
        node_id = str(n["node_id"])
        mint(chain_value_id(chain_id, node_id, "strength"), n.get("strength"), "int",
             f"the strength the executor gave node {node_id}, 0 to 5")
        nodes.append({
            "node_id": node_id, "segment_id": n.get("segment_id"), "kind": n.get("kind"),
            "criterion": n.get("criterion"), "status": n.get("status"), "strength": n.get("strength"),
            "value_ids": list(n.get("value_ids_json") or []), "expert_ids": list(n.get("expert_ids_json") or []),
            "depends_on": list(n.get("depends_on_json") or []), "text": n.get("text") or "",
            "published": bool(n.get("published")), "problems": list(n.get("problems_json") or []),
            "round": n.get("round"), "attempt": n.get("attempt"),
        })
    verdicts = [
        {"round": v.get("round"), "valid": bool(v.get("valid")), "faulty": list(v.get("faulty_json") or []),
         "feedback": v.get("feedback"), "candidate_label": v.get("candidate_label"),
         "candidate_probability": v.get("candidate_probability"), "rationale": v.get("rationale")}
        for v in loaded["verdicts"]
    ]

    served_decision = None
    if decision is not None:
        answer = decision.get("adjudicator_json") or {}
        mint(chain_value_id(chain_id, "decision_probability"), answer.get("probability"), "ratio3",
             "decider (b): the adjudicator's probability")
        served_decision = {
            "verdict": answer.get("verdict"), "probability": answer.get("probability"),
            "claims": [{"text": c.get("text") or "", "value_ids": list(c.get("value_ids") or [])}
                       for c in (decision.get("claims_json") or [])],
            "unknown_criteria": list(answer.get("unknown_criteria") or []),
            "absent_criteria": list(answer.get("absent_criteria") or []),
            "next_observation": answer.get("next_observation"), "rationale": answer.get("rationale"),
            "published": bool(decision.get("published")), "problems": list(decision.get("problems_json") or []),
        }
        # the decision carries the values it cited so the chain is self-contained; only entries that are value
        # records (the registry's rule is that the key is the record's id) are served, because one malformed
        # entry would fail the whole record against the web contract
        cited = decision.get("values_json")
        if isinstance(cited, dict):
            values |= {k: v for k, v in cited.items() if isinstance(v, dict) and v.get("id") == k}

    record = {
        **{k: chain.get(k) for k in ("chain_id", "run_id", "arm", "purpose", "planner", "rounds", "final_verdict",
                                     "final_probability", "weighted_score", "weights_version", "verifier_label",
                                     "majority_label", "abstained_reason", "created_at", "cost_usd")},
        "valid": bool(chain.get("valid")), "published": bool(chain.get("published")),
        "nodes": nodes, "verdicts": verdicts, "decision": served_decision,
    }
    return record, values


def _chains(con: duckdb.DuckDBPyConnection, cell_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The cell's newest chains as the record serves them, each with the values it prints through.

    A store from before the chain tables existed serves the rest of the record with no chains rather than
    failing: the panel's empty state is the right thing to show there, and the scores and memos still stand."""
    try:
        summaries = C.chains_for_cell(con, cell_id)[:CHAIN_LIMIT]
    except duckdb.CatalogException:
        return []
    return [_chain_record(C.load_chain(con, str(s["chain_id"]))) for s in summaries]



def candidates(limit: int = 40, model: str = "criteria") -> list[dict[str, Any]]:
    """Cells worth looking at, with the context that says whether the score means anything."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            """
            with lab as (
              select c.lon, c.lat from derived.cell_label l join derived.cell c using (cell_id)
              where l.label_tier <> 'unlabelled'
            )
            select s.cell_id, round(s.score, 4) as score, round(s.known_share, 3) as known_share,
                   c.lon, c.lat, c.in_basin, l.label_tier, l.label_name,
                   round(min(6371.0 * 2 * asin(sqrt(
                     pow(sin(radians(c.lat - lab.lat) / 2), 2) +
                     cos(radians(lab.lat)) * cos(radians(c.lat)) *
                     pow(sin(radians(c.lon - lab.lon) / 2), 2)))), 2) as km_to_label
            from derived.cell_score s
            join derived.cell c using (cell_id)
            join derived.cell_label l using (cell_id), lab
            where s.model = ? and s.score is not null
            group by s.cell_id, s.score, s.known_share, c.lon, c.lat, c.in_basin, l.label_tier, l.label_name
            order by 2 desc limit ?
            """,
            [model, limit],
        ).fetchall()
    finally:
        con.close()
    return [
        {"cell_id": r[0], "score": r[1], "known_share": r[2], "lon": r[3], "lat": r[4],
         "in_basin": bool(r[5]), "label_tier": r[6], "label_name": r[7], "km_to_label": r[8]}
        for r in rows
    ]


def evidence(cell_id: str) -> dict[str, Any]:
    """The whole evidence record for one cell: exactly what the chat agent is given, plus the analyst chains
    stored for it (computed offline for the enabled cells, served as-is), newest first."""
    parts = {name: T.call(name, {"cell_id": cell_id}).as_json()
             for name in ("cell_scores", "cell_features", "criteria_breakdown", "label_context")}
    values: dict[str, Any] = {}
    for part in parts.values():
        values |= part.get("values") or {}
    con = connect(read_only=True)
    try:
        memos = con.execute(
            "select memo_id, role, verdict, published, created_at from agent.memo "
            "where cell_id = ? order by created_at desc, role", [cell_id]
        ).fetchall()
        claims = con.execute(
            "select c.memo_id, c.claim_no, c.text, c.value_ids from agent.memo_claim c "
            "join agent.memo m using (memo_id) where m.cell_id = ? order by c.memo_id, c.claim_no",
            [cell_id],
        ).fetchall()
        cell = con.execute(
            "select lon, lat, in_basin from derived.cell where cell_id = ?", [cell_id]
        ).fetchone()
        chains = _chains(con, cell_id)
    finally:
        con.close()
    by_memo: dict[str, list[dict[str, Any]]] = {}
    for memo_id, claim_no, text, value_ids in claims:
        by_memo.setdefault(memo_id, []).append(
            {"claim_no": claim_no, "text": text, "value_ids": json.loads(value_ids or "[]")}
        )
    # a value the tools returned for this request is the same number the chain cited, read fresh; the chain's
    # own copy fills in only the ids the tools no longer serve, so every id the panel shows still resolves
    for _record, chain_values in chains:
        for vid, val in chain_values.items():
            values.setdefault(vid, val)
    return {
        "cell_id": cell_id,
        "lon": cell[0] if cell else None,
        "lat": cell[1] if cell else None,
        "in_basin": bool(cell[2]) if cell else None,
        "parts": parts,
        "values": values,
        "memos": [
            {"memo_id": m[0], "role": m[1], "verdict": m[2], "published": bool(m[3]), "created_at": m[4],
             "claims": by_memo.get(m[0], [])}
            for m in memos
        ],
        "chains": [record for record, _values in chains],
    }


def make_backend(kind: str = "openai", model: str = "") -> tuple[Any, str]:
    """Pick the backend the chat runs on, and the model name that goes with it.

    `openai` and `claude` are the chat's original two. `auto` is the interface agent's: a
    `vendor/model` id goes to OpenRouter and a bare `claude-*` id to the CLI, and the model defaults to the
    cheap interface model (`UE_INTERFACE_MODEL`, else `z-ai/glm-5.3-flash`). The caches never mix because
    the cache key carries the backend family.
    """
    from ..backends.cache import CachedBackend

    if kind == "claude":
        from ..backends.claude_cli import ClaudeCliBackend

        return CachedBackend(ClaudeCliBackend(timeout_s=600, max_budget_usd=1.20)), model or "claude-sonnet-5"

    if kind == "auto":
        from ..backends.openrouter import OpenRouterBackend, is_openrouter_model
        from ..backends.router import RoutedBackend
        from ..interface import default_model

        # The CLI is the default route only where its binary exists. A container has none, and a service
        # whose chat runs on a vendor/model id must not fail at start over a route nothing will take.
        from ..backends.router import cli_or_missing

        def make_cli() -> Any:
            from ..backends.claude_cli import ClaudeCliBackend

            return ClaudeCliBackend(timeout_s=600, max_budget_usd=1.20)

        routed = RoutedBackend([(is_openrouter_model, OpenRouterBackend(timeout_s=180))], default=cli_or_missing(make_cli))
        return CachedBackend(routed), model or default_model()

    if kind != "openai":
        raise ValueError(f"unknown backend {kind!r}: use openai, claude or auto")
    from ..backends.openai_api import OpenAIBackend, model_name

    return CachedBackend(OpenAIBackend()), model or model_name()


def serve(port: int = DEFAULT_PORT, model: str = "", effort: str = "medium",
          backend: str = "openai", log: Callable[[str], None] = print, host: str = "127.0.0.1",
          web_dist: str | None = None) -> None:
    """Run the FastAPI service on localhost. The routes and the NDJSON events are unchanged from the stdlib
    server this replaced; conversations are now persisted turn by turn in the agent tier."""
    import os

    import uvicorn

    from ..api.app import create_app

    os.environ["UE_STORE_RW"] = "1"   # one connection mode for the whole process; see store.one_mode

    chosen, model = make_backend(backend, model)
    from pathlib import Path

    dist = Path(web_dist) if web_dist else None
    app = create_app(lambda: chosen, model, effort, backend_name=backend, web_dist=dist, warm=True)
    log(f"  listening on http://{host}:{port}  ({backend}, model {model}, effort {effort})"
        + (f", serving the built site from {dist}" if dist else ""))
    log("    GET  /api/cells             candidates, highest criteria score first")
    log("    GET  /api/cell/<cell_id>    the evidence record and any stored memos")
    log("    POST /api/chat              {cell_id, question, conversation_id?}; /api/chat/stream for NDJSON")
    log("    GET  /api/conversation/<id> a persisted transcript; /docs for the OpenAPI page")
    log("  local only, and the public-safe build does not offer the chat at all")
    uvicorn.run(app, host=host, port=port, log_level="warning")
