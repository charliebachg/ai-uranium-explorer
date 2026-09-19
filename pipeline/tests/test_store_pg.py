"""The serving schema is derived from the analytics schema, not written twice.

The translation tests need no database. The round trip (migrate, sync, audit) runs only when a Postgres is
reachable at LR_PG_DSN, which is the compose stack on a developer machine and nothing on CI."""

from __future__ import annotations

import os

import pytest

from legacy_reader.store import SCHEMA_SQL
from legacy_reader.store import pg as PG


def test_every_table_and_every_tier_check_survives_the_translation() -> None:
    src = SCHEMA_SQL.read_text()
    ddl = PG.postgres_ddl(src)
    tables = PG.tables_in(src)
    assert len(tables) >= 20
    for schema, table in tables:
        assert f"create table if not exists {schema}.{table}" in ddl
    assert src.count("check (tier =") == ddl.count("check (tier ="), "no tier constraint is lost"
    assert "create or replace view" not in ddl, "views are re-stated in Postgres SQL, not translated"


def test_duckdb_types_become_postgres_types_and_nothing_else_changes() -> None:
    ddl = PG.postgres_ddl("create table if not exists derived.x (\n  a double,\n  b blob,\n  c json,\n  d text,\n  e integer,\n  f boolean,\n  g bigint, -- double in a comment stays\n  tier text not null default 'derived' check (tier = 'derived')\n);")
    assert " a double precision," in ddl and " b bytea," in ddl and " c jsonb," in ddl
    assert " d text," in ddl and " e integer," in ddl and " f boolean," in ddl and " g bigint," in ddl
    assert "-- double in a comment stays" in ddl
    assert "double precision precision" not in ddl


def test_declared_defaults_are_read_from_the_ddl() -> None:
    d = PG.declared_defaults("create table if not exists derived.x (\n  a boolean not null default false,\n  b integer not null default 0,\n  c text not null default 'k',\n  d text,\n  tier text not null default 'derived' check (tier = 'derived')\n);")
    assert d == {("derived", "x"): {"a": False, "b": 0, "c": "k"}}, "tier is the schema's business, not a fill"
    assert PG.declared_defaults()[("derived", "feature_spec")] == {"is_count": False}


def test_the_geometry_lives_only_in_postgres() -> None:
    assert "geom geometry(Polygon, 2957)" in PG.geometry_ddl()
    assert "geom geometry" not in SCHEMA_SQL.read_text(), "the analytics store keeps WKB; the geometry is a serving concern"


def _pg_reachable() -> bool:
    if not os.environ.get("LR_PG_DSN") and not os.environ.get("LR_PG_TEST"):
        return False
    try:
        PG.connect_pg().close()
        return True
    except Exception:  # noqa: BLE001
        return False


@pytest.mark.skipif(not _pg_reachable(), reason="no Postgres at LR_PG_DSN (set LR_PG_TEST=1 with the compose stack up)")
def test_round_trip_migrate_sync_audit() -> None:
    PG.migrate()
    counts = PG.sync(log=lambda *a: None)
    assert counts["derived.cell"] > 0
    conn = PG.connect_pg()
    try:
        with conn.cursor() as cur:
            cur.execute("select count(*) from derived.cell where geom is not null")
            assert cur.fetchone()[0] == counts["derived.cell"]
            cur.execute("select count(*) from derived.cell_label where label_tier = 'deposit'")
            assert cur.fetchone()[0] == 60
        assert PG.tier_audit_pg(conn) == []
    finally:
        conn.close()
