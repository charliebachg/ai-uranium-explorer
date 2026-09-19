"""The serving database: the same tiered schema in PostgreSQL + PostGIS, filled from DuckDB.

DuckDB is the right engine for feature computation and evaluation and the wrong one for concurrent serving;
Postgres is the reverse. So the tiered schema exists twice, and the Postgres copy is derived from the same
`schema.sql` by a mechanical translation, so the two cannot drift apart in the ways that matter: every table
carries the same `tier` column with the same CHECK, and the same audit runs on both. What Postgres adds is a
real geometry on `derived.cell`, so "cells in view" and "nearest label" become spatial SQL.

Nothing here computes a value. It copies rows and hashes what it copied.
"""

from __future__ import annotations

import os
import re
from typing import Any, Callable, Iterable

import pandas as pd

from . import SCHEMA_SQL, TIER_BY_SCHEMA, connect

DEFAULT_DSN = "postgresql://lr:lr@127.0.0.1:5432/lr"
SRID = 2957   # NAD83(CSRS) / UTM zone 13N, the grid's CRS

_TYPES = {"double": "double precision", "blob": "bytea", "json": "jsonb"}
_VIEW = re.compile(r"create or replace view .*?;\s*", re.S | re.I)
_TABLE = re.compile(r"(create table if not exists\s+(\w+)\.(\w+)\s*\(.*?\);)", re.S | re.I)
_DEFAULTED = re.compile(r"^\s*(\w+)\s+\w+(?:\s+\w+)?\s+not null default\s+(true|false|\d+(?:\.\d+)?|'[^']*')", re.I | re.M)


def declared_defaults(sql: str | None = None) -> dict[tuple[str, str], dict[str, Any]]:
    """Per table, the columns declared `not null default <literal>`, with the literal as a Python value.

    A column added to a live DuckDB store by a later migration is nullable there, so rows from before the
    migration carry null where the schema says false. Postgres enforces the declaration; the sync fills the
    declared default and says how many rows it touched."""
    text = sql if sql is not None else SCHEMA_SQL.read_text()
    out: dict[tuple[str, str], dict[str, Any]] = {}
    for m in _TABLE.finditer(text):
        body = m.group(1)
        cols: dict[str, Any] = {}
        for cm in _DEFAULTED.finditer(body):
            name, lit = cm.group(1), cm.group(2)
            if name == "tier":
                continue
            value: Any
            if lit.lower() in ("true", "false"):
                value = lit.lower() == "true"
            elif lit.startswith("'"):
                value = lit.strip("'")
            else:
                value = float(lit) if "." in lit else int(lit)
            cols[name] = value
        if cols:
            out[(m.group(2), m.group(3))] = cols
    return out


def pg_dsn() -> str:
    return os.environ.get("LR_PG_DSN", "").strip() or DEFAULT_DSN


def postgres_ddl(sql: str | None = None) -> str:
    """schema.sql translated for Postgres: schemas, tables with their tier CHECKs, DuckDB types mapped.

    Views are left out; the two the store has are re-stated in `postgres_views()` in plain SQL. Comments are
    kept, so the Postgres schema reads the same as the DuckDB one."""
    text = sql if sql is not None else SCHEMA_SQL.read_text()
    text = _VIEW.sub("", text)
    out = []
    for line in text.splitlines():
        code, _, comment = line.partition("--")
        for src, dst in _TYPES.items():
            code = re.sub(rf"(?<=\s){src}(?=[\s,)])", dst, code)
        out.append(code + ("--" + comment if comment else ""))
    return "\n".join(out)


def postgres_views() -> str:
    return """
create or replace view derived.v_feature_matrix as
  select c.cell_id, c.lon, c.lat, c.in_basin, f.feature_key, f.value, f.n_obs, f.from_tier, f.tier
  from derived.cell c left join derived.cell_feature f using (cell_id);
create or replace view native.v_layer_summary as
  select layer_key, title, role, record_count, licence, redistributable, retrieved_at, tier
  from native.layer;
"""


def geometry_ddl() -> str:
    """The one thing Postgres has that DuckDB does not: a real cell geometry with a spatial index."""
    return f"""
create extension if not exists postgis;
alter table derived.cell add column if not exists geom geometry(Polygon, {SRID});
create index if not exists cell_geom_gist on derived.cell using gist (geom);
create index if not exists cell_label_tier_idx on derived.cell_label (label_tier);
create index if not exists cell_score_model_idx on derived.cell_score (model, cell_id);
create index if not exists cell_feature_key_idx on derived.cell_feature (feature_key, cell_id);
"""


def tables_in(sql: str | None = None) -> list[tuple[str, str]]:
    text = sql if sql is not None else SCHEMA_SQL.read_text()
    return [(m.group(2), m.group(3)) for m in _TABLE.finditer(text)]


def connect_pg(dsn: str | None = None):
    import psycopg

    return psycopg.connect(dsn or pg_dsn())


def tier_audit_pg(conn: Any) -> list[str]:
    """The DuckDB audit, verbatim in meaning, on Postgres: every tiered table carries its schema's tier only."""
    problems: list[str] = []
    with conn.cursor() as cur:
        cur.execute("select table_schema, table_name from information_schema.tables "
                    "where table_schema in ('native','read','derived','agent') and table_type = 'BASE TABLE' order by 1, 2")
        tables = cur.fetchall()
        for schema, table in tables:
            cur.execute("select column_name from information_schema.columns where table_schema = %s and table_name = %s",
                        [schema, table])
            cols = {r[0] for r in cur.fetchall()}
            if "tier" not in cols:
                problems.append(f"{schema}.{table}: no tier column")
                continue
            cur.execute(f'select distinct tier from "{schema}"."{table}"')
            found = {r[0] for r in cur.fetchall() if r[0] is not None}
            wrong = found - {TIER_BY_SCHEMA[schema]}
            if wrong:
                problems.append(f"{schema}.{table}: holds tier(s) {sorted(wrong)}, expected {TIER_BY_SCHEMA[schema]}")
    return problems


def _rows(df: pd.DataFrame) -> Iterable[tuple]:
    clean = df.astype(object).where(pd.notna(df), None)
    for row in clean.itertuples(index=False, name=None):
        yield tuple(v.item() if hasattr(v, "item") else v for v in row)


def sync(dsn: str | None = None, tables: list[tuple[str, str]] | None = None,
         log: Callable[[str], None] = print) -> dict[str, int]:
    """Copy every tiered table from DuckDB into Postgres (truncate, then COPY), set the cell geometry, audit."""
    wanted = tables or tables_in()
    duck = connect(read_only=True)
    conn = connect_pg(dsn)
    counts: dict[str, int] = {}
    try:
        with conn.cursor() as cur:
            for schema, table in wanted:
                try:
                    df = duck.execute(f"select * from {schema}.{table}").df()
                except Exception as e:  # noqa: BLE001 - a table the DuckDB store never built is skipped, and said so
                    log(f"  {schema}.{table}: not in the analytics store ({type(e).__name__}); skipped")
                    continue
                if "tier" in df.columns and len(df):
                    present = set(df["tier"].dropna().unique())
                    if present - {TIER_BY_SCHEMA[schema]}:
                        raise RuntimeError(f"{schema}.{table} carries tier(s) {sorted(present)}; refusing to copy")
                for col, value in declared_defaults().get((schema, table), {}).items():
                    if col in df.columns and df[col].isna().any():
                        n = int(df[col].isna().sum())
                        df[col] = df[col].fillna(value)
                        log(f"  {schema}.{table}.{col}: {n} null(s) filled with the declared default {value!r}")
                cols = [c for c in df.columns]
                cur.execute(f'truncate table "{schema}"."{table}"')
                if len(df):
                    collist = ", ".join(f'"{c}"' for c in cols)
                    with cur.copy(f'copy "{schema}"."{table}" ({collist}) from stdin') as copy:
                        for row in _rows(df[cols]):
                            copy.write_row(row)
                counts[f"{schema}.{table}"] = len(df)
                log(f"  {schema}.{table}: {len(df):,} rows")
            cur.execute("select count(*) from information_schema.columns where table_schema='derived' and table_name='cell' and column_name='geom'")
            if cur.fetchone()[0]:
                cur.execute(f"update derived.cell set geom = ST_SetSRID(ST_GeomFromWKB(geom_wkb), {SRID}) where geom_wkb is not null")
                log(f"  derived.cell.geom set from geom_wkb ({cur.rowcount:,} cells)")
        conn.commit()
        problems = tier_audit_pg(conn)
    finally:
        conn.close()
        duck.close()
    if problems:
        raise RuntimeError("postgres refused: tier audit failed\n  " + "\n  ".join(problems))
    log(f"  tier audit clean across {len(counts)} tables in Postgres")
    return counts


def migrate(dsn: str | None = None, revision: str = "head") -> None:
    """Run the Alembic migrations against the serving database."""
    from pathlib import Path

    from alembic import command
    from alembic.config import Config

    ini = Path(__file__).resolve().parents[3] / "alembic.ini"
    cfg = Config(str(ini))
    cfg.set_main_option("script_location", str(ini.parent / "migrations"))
    cfg.set_main_option("sqlalchemy.url", (dsn or pg_dsn()).replace("postgresql://", "postgresql+psycopg://"))
    command.upgrade(cfg, revision)
