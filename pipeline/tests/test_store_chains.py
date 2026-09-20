"""Analyst chains land in the agent tier whole, gated once more at the store, and the serving schema matches."""

from __future__ import annotations

import importlib.util
import re
from pathlib import Path
from typing import Any

import pandas as pd
import pytest

from legacy_reader.analyst import chains as C
from legacy_reader.store import SCHEMA_SQL, TierError, connect, insert_frame, tier_audit
from legacy_reader.store import pg as PG

TABLES = ("chain", "chain_node", "chain_verdict", "chain_decision")
MIGRATION = Path(__file__).resolve().parents[1] / "migrations" / "versions" / "0004_agent_chain.py"


@pytest.fixture
def con(tmp_path):
    c = connect(tmp_path / "s.duckdb")
    yield c
    c.close()


VALUES = {
    "v:cond": {"value_id": "v:cond", "value": 1200, "unit": "m", "as_printed": "1200"},
    "v:fault": {"value_id": "v:fault", "value": 3.5, "unit": "km"},
}


def _chain(**over: Any) -> dict[str, Any]:
    row = {
        "chain_id": "ch1", "cell_id": "0201_0072", "bench_id": None, "purpose": "dashboard",
        "run_id": "20260920T120000Z-analyst", "arm": "v1-template", "fold": None, "planner": "template",
        "rounds": 2, "valid": True, "final_verdict": "supports_closer_look", "final_probability": 0.61,
        "weighted_score": 0.58, "weights_version": "w/v1", "verifier_label": "supports_closer_look",
        "majority_label": None, "abstained_reason": None, "published": True,
        "models_json": {"executor": "m-small", "verifier": "m-large", "adjudicator": "m-large"},
        "manifest_sha256": "abc", "blind_list_hash": None, "cost_usd": 0.12, "duration_s": 41.0,
        "created_at": "2026-09-20T12:00:00+00:00",
    }
    row.update(over)
    return row


def _node(node_id: str, round_: int = 0, attempt: int = 1, **over: Any) -> dict[str, Any]:
    row = {
        "node_id": node_id, "round": round_, "attempt": attempt, "segment_id": f"seg-{node_id}",
        "kind": "criterion", "criterion": "conductor_distance", "status": "met", "strength": 4,
        "value_ids_json": ["v:cond"], "expert_ids_json": [], "depends_on_json": [],
        "text": "A conductor lies 1200 m from the cell centre.", "published": True, "problems_json": [],
        "model": "m-small", "cost_usd": 0.01, "duration_s": 2.0, "created_at": "2026-09-20T12:00:01+00:00",
    }
    row.update(over)
    return row


def _verdict(round_: int, valid: bool, **over: Any) -> dict[str, Any]:
    row = {
        "round": round_, "valid": valid, "faulty_json": [] if valid else [{"node_id": "n01", "reason": "polarity"}],
        "feedback": None if valid else "n01 reads a favourable feature as not met", "candidate_label": "supports_closer_look",
        "candidate_probability": 0.6, "rationale": "the nodes agree", "model": "m-large", "cost_usd": 0.05,
        "duration_s": 9.0, "created_at": f"2026-09-20T12:00:1{round_}+00:00",
    }
    row.update(over)
    return row


def _decision(**over: Any) -> dict[str, Any]:
    claims = [{"text": "The nearest conductor is 1.2 km away.", "value_ids": ["v:cond"]}]
    row = {
        "adjudicator_json": {"verdict": "supports_closer_look", "probability": 0.61, "claims": claims,
                             "unknown_criteria": ["alteration"], "absent_criteria": [],
                             "next_observation": "a resistivity line across the conductor", "rationale": "..."},
        "claims_json": claims, "values_json": VALUES, "published": True, "problems_json": [],
        "model": "m-large", "cost_usd": 0.04, "duration_s": 7.0, "created_at": "2026-09-20T12:00:30+00:00",
    }
    row.update(over)
    return row


def _store(con, **chain_over: Any) -> str:
    nodes = [_node("n01", 0, 1, status="not_met", published=False, problems_json=["polarity"]),
             _node("n01", 0, 2), _node("n02", 0, 1, criterion="fault_distance", value_ids_json=["v:fault"],
                                       text="The nearest fault is 3.5 km away."),
             _node("n01", 1, 1, strength=3),
             _node("n03", 1, 1, kind="crosscheck", criterion=None, depends_on_json=["n01", "n02"],
                   value_ids_json=["v:cond", "v:fault"], text="Conductor and fault coincide.")]
    return C.store_chain(con, _chain(**chain_over), nodes, [_verdict(0, False), _verdict(1, True)], _decision())


# ---------------------------------------------------------------- the tables


def test_the_four_tables_exist_in_the_agent_tier_and_the_audit_is_clean(con) -> None:
    for table in TABLES:
        cols = {c[0] for c in con.execute(f"describe agent.{table}").fetchall()}
        assert "tier" in cols, f"agent.{table} has no tier column"
    _store(con)
    assert tier_audit(con) == []
    assert con.execute("select distinct tier from agent.chain_node").fetchall() == [("agent",)]


def test_a_derived_frame_cannot_land_in_the_chain_table(con) -> None:
    with pytest.raises(TierError):
        insert_frame(con, "agent", "chain", pd.DataFrame([_chain(models_json="{}")]), "derived")


# ---------------------------------------------------------------- store and load


def test_store_chain_round_trips_through_load_chain(con) -> None:
    chain_id = _store(con)
    got = C.load_chain(con, chain_id)
    assert got["chain"]["chain_id"] == "ch1" and got["chain"]["tier"] == "agent"
    assert got["chain"]["models_json"] == {"executor": "m-small", "verifier": "m-large", "adjudicator": "m-large"}
    assert got["chain"]["fold"] is None and got["chain"]["valid"] is True and got["chain"]["final_probability"] == 0.61
    # nodes in (round, attempt, node_id) order, every attempt kept, json decoded
    assert [(n["round"], n["attempt"], n["node_id"]) for n in got["nodes"]] == [
        (0, 1, "n01"), (0, 1, "n02"), (0, 2, "n01"), (1, 1, "n01"), (1, 1, "n03")]
    assert got["nodes"][0]["problems_json"] == ["polarity"] and got["nodes"][0]["published"] is False
    assert got["nodes"][-1]["depends_on_json"] == ["n01", "n02"] and got["nodes"][-1]["criterion"] is None
    assert [v["round"] for v in got["verdicts"]] == [0, 1]
    assert got["verdicts"][0]["faulty_json"] == [{"node_id": "n01", "reason": "polarity"}]
    assert got["decision"]["values_json"] == VALUES
    assert got["decision"]["adjudicator_json"]["next_observation"] == "a resistivity line across the conductor"
    # the current nodes are the last attempt of each id
    assert [(n["node_id"], n["round"], n["attempt"]) for n in C.current_nodes(got["nodes"])] == [
        ("n01", 1, 1), ("n02", 0, 1), ("n03", 1, 1)]


def test_a_chain_without_a_decision_is_stored_as_an_abstention(con) -> None:
    C.store_chain(con, _chain(chain_id="ch-abstain", valid=False, final_verdict="insufficient",
                              final_probability=None, abstained_reason="never validated in 2 rounds"),
                  [_node("n01")], [_verdict(0, False), _verdict(1, False)], None)
    got = C.load_chain(con, "ch-abstain")
    assert got["decision"] is None and got["chain"]["abstained_reason"] == "never validated in 2 rounds"


def test_a_missing_chain_is_a_key_error(con) -> None:
    with pytest.raises(KeyError):
        C.load_chain(con, "nope")


# ---------------------------------------------------------------- the gate


def test_a_published_chain_with_an_unpublished_current_node_is_refused(con) -> None:
    nodes = [_node("n01", 0, 1), _node("n01", 0, 2, published=False, problems_json=["cites an id not returned"])]
    with pytest.raises(C.ChainRefused, match="n01 .*round 0, attempt 2.* did not pass"):
        C.store_chain(con, _chain(), nodes, [_verdict(0, True)], _decision())
    assert con.execute("select count(*) from agent.chain").fetchone()[0] == 0, "nothing written"
    # the same chain, unpublished, is stored as the record of the refusal
    C.store_chain(con, _chain(published=False), nodes, [_verdict(0, True)], _decision())
    assert C.load_chain(con, "ch1")["chain"]["published"] is False


def test_a_decision_claim_citing_an_unknown_id_is_refused_by_name(con) -> None:
    decision = _decision(claims_json=[{"text": "A conductor lies 1200 m away.", "value_ids": ["v:ghost"]}])
    with pytest.raises(C.ChainRefused, match="v:ghost"):
        C.store_chain(con, _chain(), [_node("n01")], [_verdict(0, True)], decision)
    assert con.execute("select count(*) from agent.chain").fetchone()[0] == 0


def test_a_decision_number_no_cited_value_backs_is_refused(con) -> None:
    decision = _decision(claims_json=[{"text": "The conductor is 950 m away.", "value_ids": ["v:cond"]}])
    with pytest.raises(C.ChainRefused, match="950"):
        C.store_chain(con, _chain(), [_node("n01")], [_verdict(0, True)], decision)


def test_a_verdict_outside_the_protocol_is_refused(con) -> None:
    with pytest.raises(C.ChainRefused, match="final_verdict"):
        C.store_chain(con, _chain(final_verdict="maybe"), [_node("n01")], [_verdict(0, True)], _decision())
    with pytest.raises(C.ChainRefused, match="status"):
        C.store_chain(con, _chain(), [_node("n01", status="not met")], [_verdict(0, True)], _decision())


def test_storing_the_same_chain_twice_is_refused_and_leaves_one_copy(con) -> None:
    _store(con)
    with pytest.raises(C.ChainRefused, match="already stored"):
        _store(con)
    assert con.execute("select count(*) from agent.chain_node").fetchone()[0] == 5


# ---------------------------------------------------------------- listing and deleting


def test_chains_for_cell_orders_newest_first_and_filters_by_purpose(con) -> None:
    _store(con, chain_id="a", created_at="2026-09-20T10:00:00+00:00")
    _store(con, chain_id="b", created_at="2026-09-20T11:00:00+00:00", purpose="scored", run_id="r2")
    _store(con, chain_id="c", created_at="2026-09-20T12:00:00+00:00")
    _store(con, chain_id="other", cell_id="0201_0073")
    assert [r["chain_id"] for r in C.chains_for_cell(con, "0201_0072")] == ["c", "b", "a"]
    assert [r["chain_id"] for r in C.chains_for_cell(con, "0201_0072", purpose="scored")] == ["b"]
    assert C.chains_for_cell(con, "0201_0072")[0]["models_json"]["verifier"] == "m-large"
    assert C.chains_for_cell(con, "nowhere") == []


def test_delete_run_removes_exactly_that_run_from_all_four_tables(con) -> None:
    _store(con, chain_id="a")
    _store(con, chain_id="b", run_id="r2")
    assert C.delete_run(con, "20260920T120000Z-analyst") == 1
    for table in TABLES:
        left = {r[0] for r in con.execute(f"select distinct chain_id from agent.{table}").fetchall()}
        assert left == {"b"}, f"agent.{table} still holds {left}"
    assert C.delete_run(con, "r-none") == 0


# ---------------------------------------------------------------- the serving schema


def _columns(sql: str) -> dict[str, list[tuple[str, str]]]:
    """Per chain table, (column, first type word) in order, from a create-table body: enough to hold the two
    schemas to the same columns without parsing SQL."""
    out: dict[str, list[tuple[str, str]]] = {}
    for m in re.finditer(r"create table if not exists\s+agent\.(chain\w*)\s*\((.*?)\n\);", sql, re.S):
        cols = []
        for line in m.group(2).splitlines():
            code = line.partition("--")[0].strip()
            if not code or code.startswith("primary key"):
                continue
            name, kind = code.split()[:2]
            cols.append((name, kind))
        out[m.group(1)] = cols
    return out


def test_migration_0004_creates_the_same_tables_and_columns_as_schema_sql(monkeypatch) -> None:
    spec = importlib.util.spec_from_file_location("lr_migration_0004", MIGRATION)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    assert mod.revision == "0004" and mod.down_revision == "0003"
    executed: list[str] = []
    monkeypatch.setattr(mod.op, "execute", executed.append)
    mod.upgrade()
    migrated = _columns("\n".join(executed))
    declared = _columns(PG.postgres_ddl(SCHEMA_SQL.read_text()))
    assert set(declared) == set(TABLES) and set(migrated) == set(TABLES)
    for table in TABLES:
        assert migrated[table] == declared[table], f"agent.{table} differs between schema.sql and 0004"
        assert ("tier", "text") in migrated[table]
    assert "double precision" in "\n".join(executed) and " double," not in "\n".join(executed)
    assert "chain_cell_idx" in "\n".join(executed) and "chain_run_idx" in "\n".join(executed)
    mod.downgrade()
    assert all(f"drop table if exists agent.{t}" in executed[-1] for t in TABLES)
