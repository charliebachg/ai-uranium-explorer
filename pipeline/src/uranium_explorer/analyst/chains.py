"""Storage for Analyst v1 chains: the loop's last stage, the publish step.

A chain is what the staged loop produced over one cell: the nodes each segment's executor returned (every
attempt, every round, so a repaired node keeps its history), the verifier's verdict per round, and the
adjudicator's decision. All of it lands in the `agent` tier through `append_frame`, so the tier CHECK holds and
nothing here can be read as a number: a node cites value ids, and the decision carries the cited values
themselves, so a chain can be re-scored later without the store it ran against.

The store is the last gate. The loop gates each node mechanically and the decision's claims by the memo rule,
and marks what passed; this module refuses to publish a chain as a whole when any part of it did not, and
re-runs the claim check on the decision so a chain cannot be marked published by a caller that skipped it.
A refused chain is not written at all - the caller marks it unpublished and stores it as the record of the
refusal, which is what the benchmark counts as an abstention.

The runtime is not imported here: `store_chain` takes plain dicts keyed by column name, with the `_json`
columns given as Python objects, and `load_chain` hands the same shape back.
"""

from __future__ import annotations

import json
from contextlib import contextmanager
from typing import Any, Iterator

import duckdb
import pandas as pd

from ..prospect.memo import check_claims, quotable
from ..store import append_frame, connect

PURPOSES = ("dashboard", "scored", "benchmark")
PLANNERS = ("template", "model")
VERDICTS = ("evidence_against", "insufficient", "supports_closer_look")
NODE_KINDS = ("criterion", "crosscheck", "retrieval")
NODE_STATUSES = ("met", "not_met", "unknown")

CHAIN_COLUMNS = (
    "chain_id", "cell_id", "bench_id", "purpose", "run_id", "arm", "fold", "planner", "rounds", "valid",
    "final_verdict", "final_probability", "weighted_score", "weights_version", "verifier_label",
    "majority_label", "abstained_reason", "published", "models_json", "manifest_sha256", "blind_list_hash",
    "cost_usd", "duration_s", "created_at",
)
NODE_COLUMNS = (
    "chain_id", "node_id", "round", "attempt", "segment_id", "kind", "criterion", "status", "strength",
    "value_ids_json", "expert_ids_json", "depends_on_json", "text", "published", "problems_json", "model",
    "cost_usd", "duration_s", "created_at",
)
VERDICT_COLUMNS = (
    "chain_id", "round", "valid", "faulty_json", "feedback", "candidate_label", "candidate_probability",
    "rationale", "model", "cost_usd", "duration_s", "created_at",
)
DECISION_COLUMNS = (
    "chain_id", "adjudicator_json", "claims_json", "values_json", "published", "problems_json", "model",
    "cost_usd", "duration_s", "created_at",
)

#: the columns that hold JSON: encoded on the way in, decoded on the way out
CHAIN_JSON = ("models_json",)
NODE_JSON = ("value_ids_json", "expert_ids_json", "depends_on_json", "problems_json")
VERDICT_JSON = ("faulty_json",)
DECISION_JSON = ("adjudicator_json", "claims_json", "values_json", "problems_json")

#: (table, columns, json columns) for the four tables, in the order they are written and deleted
TABLES: tuple[tuple[str, tuple[str, ...], tuple[str, ...]], ...] = (
    ("chain", CHAIN_COLUMNS, CHAIN_JSON),
    ("chain_node", NODE_COLUMNS, NODE_JSON),
    ("chain_verdict", VERDICT_COLUMNS, VERDICT_JSON),
    ("chain_decision", DECISION_COLUMNS, DECISION_JSON),
)


class ChainRefused(ValueError):
    """The store would not publish this chain; the message says which part failed."""


# ---------------------------------------------------------------- encoding


def _encode(value: Any) -> str:
    """A JSON column's value as text. A string is taken as already encoded and checked, so a caller that
    encoded once is not encoded twice, and a blob that does not parse is refused here rather than by a reader."""
    if isinstance(value, str):
        json.loads(value)
        return value
    return json.dumps(value)


def _frame(rows: list[dict[str, Any]], columns: tuple[str, ...], json_columns: tuple[str, ...]) -> pd.DataFrame:
    """Rows as a frame in column order, missing keys as null: the schema's NOT NULLs decide what is required,
    so a node written without its `problems_json` is refused by the database rather than filled in here."""
    out = []
    for row in rows:
        rec = {c: row.get(c) for c in columns}
        for c in json_columns:
            if rec[c] is not None:
                rec[c] = _encode(rec[c])
        out.append(rec)
    return pd.DataFrame(out, columns=list(columns))


def _decode(row: dict[str, Any], json_columns: tuple[str, ...]) -> dict[str, Any]:
    for c in json_columns:
        if row.get(c) is not None:
            row[c] = json.loads(row[c])
    return row


def _rows(cur: duckdb.DuckDBPyConnection, json_columns: tuple[str, ...]) -> list[dict[str, Any]]:
    names = [d[0] for d in cur.description]
    return [_decode(dict(zip(names, r)), json_columns) for r in cur.fetchall()]


@contextmanager
def _connection(con: duckdb.DuckDBPyConnection | None) -> Iterator[duckdb.DuckDBPyConnection]:
    """The caller's connection, or the project store opened for this call and closed after it."""
    if con is not None:
        yield con
        return
    own = connect()
    try:
        yield own
    finally:
        own.close()


# ---------------------------------------------------------------- the gate


def _check_enum(what: str, value: Any, allowed: tuple[str, ...]) -> None:
    if value not in allowed:
        raise ChainRefused(f"{what} {value!r} is not one of {allowed}")


def current_nodes(nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last attempt of each node id, in plan order: what the verdict and the decision were read from.

    A node re-executed after the verifier faulted it, or retried after the mechanical gate rejected it, keeps
    every earlier attempt in the store; only the latest (round, attempt) is the node as the chain stands."""
    latest: dict[str, dict[str, Any]] = {}
    for n in nodes:
        key = str(n.get("node_id"))
        prev = latest.get(key)
        if prev is None or (int(n.get("round") or 0), int(n.get("attempt") or 0)) > (
            int(prev.get("round") or 0), int(prev.get("attempt") or 0)
        ):
            latest[key] = n
    return [latest[k] for k in sorted(latest)]


def _as_values(values: Any) -> dict[str, dict[str, Any]]:
    """`values_json` keyed by value id. The loop hands over a dict; a list of value rows is keyed here."""
    if isinstance(values, dict):
        return values
    return {str(v.get("value_id")): v for v in (values or [])}


def _refusals(nodes: list[dict[str, Any]], decision: dict[str, Any] | None, context: str = "") -> list[str]:
    """Why a chain marked published may not be. Empty when it may."""
    problems: list[str] = []
    for n in current_nodes(nodes):
        if not bool(n.get("published")):
            problems.append(f"node {n.get('node_id')} (round {n.get('round')}, attempt {n.get('attempt')}) "
                            f"did not pass the node gate")
    if decision is not None:
        if not bool(decision.get("published")):
            problems.append("the decision did not pass its gate")
        values = _as_values(decision.get("values_json"))
        # The same rule the loop's gate applied: a number in a claim comes from a value the claim cites, or is
        # a string the tools returned in that session (`context`, the loop's own transcript; a map scale, a
        # hole name). Without the transcript the allowance is only the strings the cited values carry, which
        # once refused a chain the loop had published for quoting "1:250,000" from a caveat.
        problems += [f"decision {p}" for p in
                     check_claims(list(decision.get("claims_json") or []), values,
                                  context=(context + "\n" + quotable(values)) if context else quotable(values))]
    return problems


# ---------------------------------------------------------------- the API


def store_chain(
    con: duckdb.DuckDBPyConnection | None, chain: dict[str, Any], nodes: list[dict[str, Any]],
    verdicts: list[dict[str, Any]], decision: dict[str, Any] | None, context: str = "",
) -> str:
    """Write a chain to the four `agent.chain*` tables in one transaction and return its id.

    Refuses, writing nothing, a chain marked published while any current node or the decision did not pass
    its gate, or whose decision claims cite an id that is not in `values_json` or state a number no cited
    value backs and the tool transcript (`context`, what the loop's session returned as text) does not carry.
    The enumerated fields are checked whatever the published flag says, because a dashboard filters on them
    and a misspelt verdict would simply vanish from it."""
    chain_id = str(chain.get("chain_id") or "")
    if not chain_id:
        raise ChainRefused("a chain needs a chain_id")
    _check_enum("purpose", chain.get("purpose"), PURPOSES)
    _check_enum("planner", chain.get("planner"), PLANNERS)
    _check_enum("final_verdict", chain.get("final_verdict"), VERDICTS)
    for n in nodes:
        _check_enum(f"node {n.get('node_id')} kind", n.get("kind"), NODE_KINDS)
        _check_enum(f"node {n.get('node_id')} status", n.get("status"), NODE_STATUSES)
        if not 0 <= int(n.get("strength") or 0) <= 5:
            raise ChainRefused(f"node {n.get('node_id')} strength {n.get('strength')!r} is not in 0..5")
    if bool(chain.get("published")):
        problems = _refusals(nodes, decision, context)
        if problems:
            raise ChainRefused(f"chain {chain_id} cannot be published: " + "; ".join(problems))

    frames = {
        "chain": _frame([chain], CHAIN_COLUMNS, CHAIN_JSON),
        "chain_node": _frame([{**n, "chain_id": chain_id} for n in nodes], NODE_COLUMNS, NODE_JSON),
        "chain_verdict": _frame([{**v, "chain_id": chain_id} for v in verdicts], VERDICT_COLUMNS, VERDICT_JSON),
        "chain_decision": _frame([{**decision, "chain_id": chain_id}] if decision is not None else [],
                                 DECISION_COLUMNS, DECISION_JSON),
    }
    with _connection(con) as c:
        c.begin()
        try:
            if c.execute("select 1 from agent.chain where chain_id = ?", [chain_id]).fetchone():
                raise ChainRefused(f"chain {chain_id} is already stored; delete_run() removes a run for a re-run")
            for table, _cols, _json in TABLES:
                if len(frames[table]):
                    append_frame(c, "agent", table, frames[table], "agent")
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return chain_id


def load_chain(con: duckdb.DuckDBPyConnection | None, chain_id: str) -> dict[str, Any]:
    """The chain as stored, JSON columns decoded: `{"chain", "nodes", "verdicts", "decision"}`, the same shape
    `store_chain` takes. Nodes come in (round, attempt, node_id) order, verdicts by round."""
    with _connection(con) as c:
        chains = _rows(c.execute("select * from agent.chain where chain_id = ?", [chain_id]), CHAIN_JSON)
        if not chains:
            raise KeyError(f"no chain {chain_id!r}")
        nodes = _rows(c.execute("select * from agent.chain_node where chain_id = ? "
                                "order by round, attempt, node_id", [chain_id]), NODE_JSON)
        verdicts = _rows(c.execute("select * from agent.chain_verdict where chain_id = ? order by round",
                                   [chain_id]), VERDICT_JSON)
        decisions = _rows(c.execute("select * from agent.chain_decision where chain_id = ?", [chain_id]),
                          DECISION_JSON)
    return {"chain": chains[0], "nodes": nodes, "verdicts": verdicts,
            "decision": decisions[0] if decisions else None}


def chains_for_cell(
    con: duckdb.DuckDBPyConnection | None, cell_id: str, purpose: str | None = None
) -> list[dict[str, Any]]:
    """A cell's chains, newest first, as their `agent.chain` rows: what a dashboard lists before it opens one."""
    sql = "select * from agent.chain where cell_id = ?"
    args: list[Any] = [cell_id]
    if purpose is not None:
        sql += " and purpose = ?"
        args.append(purpose)
    with _connection(con) as c:
        return _rows(c.execute(sql + " order by created_at desc, chain_id desc", args), CHAIN_JSON)


def delete_run(con: duckdb.DuckDBPyConnection | None, run_id: str) -> int:
    """Remove a run's chains from all four tables, for a re-run. Returns how many chains went."""
    with _connection(con) as c:
        c.begin()
        try:
            ids = [r[0] for r in c.execute("select chain_id from agent.chain where run_id = ?", [run_id]).fetchall()]
            for table, _cols, _json in reversed(TABLES):
                c.execute(f"delete from agent.{table} where chain_id in "
                          f"(select chain_id from agent.chain where run_id = ?)", [run_id])
            c.commit()
        except BaseException:
            c.rollback()
            raise
    return len(ids)
