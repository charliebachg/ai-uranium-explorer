"""The store's one rule: a fact never changes provenance tier, and no table mixes tiers."""

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from uranium_explorer.store import (
    TIER_BY_SCHEMA,
    TierError,
    apply_schema,
    insert_frame,
    tier_audit,
    write_meta,
)


@pytest.fixture
def con(tmp_path):
    c = duckdb.connect(str(tmp_path / "t.duckdb"))
    apply_schema(c)
    yield c
    c.close()


def test_schema_declares_five_tiers(con):
    schemas = {
        r[0]
        for r in con.execute(
            "select schema_name from information_schema.schemata where schema_name in "
            "('native','read','derived','agent','expert')"
        ).fetchall()
    }
    assert schemas == set(TIER_BY_SCHEMA)


def test_every_authored_table_has_a_tier_column_and_a_check(con):
    rows = con.execute(
        "select table_schema, table_name from information_schema.tables "
        "where table_schema in ('native','read','derived','agent') and table_type = 'BASE TABLE'"
    ).fetchall()
    assert rows, "schema.sql authored no tables"
    for schema, table in rows:
        cols = {c[0] for c in con.execute(f"describe {schema}.{table}").fetchall()}
        assert "tier" in cols, f"{schema}.{table} has no tier column"


def test_check_constraint_refuses_a_foreign_tier(con):
    con.execute(
        "insert into native.layer (layer_key, title, service_url, record_count, licence, redistributable, "
        "retrieved_at, role) values ('l1','L','http://x',1,'SK',true,'2026-01-01','feature')"
    )
    assert con.execute("select tier from native.layer").fetchone()[0] == "native"
    with pytest.raises(duckdb.ConstraintException):
        con.execute(
            "insert into native.layer (layer_key, title, service_url, record_count, licence, "
            "redistributable, retrieved_at, role, tier) "
            "values ('l2','L','http://x',1,'SK',true,'2026-01-01','feature','read')"
        )


def test_insert_frame_stamps_the_tier(con):
    n = insert_frame(con, "read", "thing", pd.DataFrame([{"a": 1}, {"a": 2}]), "read")
    assert n == 2
    assert con.execute("select distinct tier from read.thing").fetchall() == [("read",)]


def test_insert_frame_refuses_a_page_reading_in_the_native_schema(con):
    # a value read off a PDF may never be stored as something the province published
    reading = pd.DataFrame([{"value_id": "x:74H09-0039:abc", "as_printed": "190.0", "tier": "read"}])
    with pytest.raises(TierError) as e:
        insert_frame(con, "native", "feature_wrong", reading, "read")
    assert "native" in str(e.value)


def test_insert_frame_refuses_a_frame_carrying_another_tier(con):
    df = pd.DataFrame([{"a": 1, "tier": "agent"}])
    with pytest.raises(TierError):
        insert_frame(con, "derived", "thing", df, "derived")


def test_unknown_schema_is_refused(con):
    with pytest.raises(TierError):
        insert_frame(con, "public", "thing", pd.DataFrame([{"a": 1}]), "public")


def test_tier_audit_is_clean_on_a_fresh_store_and_catches_a_bad_table(con):
    write_meta(con, "0.0.0")
    assert tier_audit(con) == []
    # a table written behind the guard's back is exactly what the audit exists to catch
    con.execute("create table derived.sneaky as select 1 as a, 'read' as tier")
    problems = tier_audit(con)
    assert any("derived.sneaky" in p and "read" in p for p in problems)
    con.execute("drop table derived.sneaky")
    con.execute("create table agent.no_tier as select 1 as a")
    assert any("agent.no_tier" in p and "no tier column" in p for p in tier_audit(con))


def test_cell_feature_records_where_its_inputs_came_from(con):
    """A per-cell number must say which tier it was computed from: the readiness page reads this."""
    cols = {c[0] for c in con.execute("describe derived.cell_feature").fetchall()}
    assert {"from_tier", "op", "tool", "inputs", "n_obs"} <= cols


def test_a_cell_with_no_observations_can_hold_null_rather_than_zero(con):
    con.execute(
        "insert into derived.cell_feature (cell_id, feature_key, value, n_obs, from_tier, op, tool, "
        "computed_at) values ('c1','sed_u_ppm',null,0,'native','idw','test','2026-01-01')"
    )
    row = con.execute("select value, n_obs from derived.cell_feature").fetchone()
    assert row == (None, 0)
