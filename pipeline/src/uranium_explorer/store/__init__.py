"""The store: one DuckDB file, four provenance tiers that never mix.

`native` is what a public service returned, `read` is what OCR and the vision model made of a document page,
`derived` is what this pipeline computed, `agent` is what a model argued. The distinction is the point: a
provincial record and a model's reading of a scanned table do not deserve the same trust, and keeping them in
separate schemas stops an unvalidated number from quietly becoming a feature or a label.

Everything goes in through `insert_frame`, which refuses a frame whose tier does not match its schema.
"""

from __future__ import annotations

import datetime as dt
import os
from pathlib import Path
from typing import Any

import duckdb

from ..paths import PATHS

STORE_VERSION = "store/v2"

#: schema -> the one tier its tables may hold
TIER_BY_SCHEMA: dict[str, str] = {
    "native": "native",
    "read": "read",
    "derived": "derived",
    "agent": "agent",
    "expert": "expert",
}

SCHEMA_SQL = Path(__file__).with_name("schema.sql")


class TierError(Exception):
    """Raised when a row would land in the wrong provenance tier."""


def db_path() -> Path:
    return PATHS.data / "ue.duckdb"


#: paths whose schema this process has already applied; a serving process opens hundreds of connections and
#: need not re-run twenty CREATE IF NOT EXISTS statements on each one
_SCHEMA_APPLIED: set[str] = set()


def one_mode() -> bool:
    """True inside the serving process: every connection is read-write there.

    DuckDB refuses to open the same file read-only and read-write at once from one process, and the API does
    both by nature — tool reads for one request while another persists a conversation turn. So `ue prospect
    serve` sets UE_STORE_RW=1, and `connect(read_only=True)` then hands back a read-write connection. Batch
    commands keep their read-only connections; a read-only handle in a pipeline stage still means what it says."""
    return os.environ.get("UE_STORE_RW", "") == "1"


def connect(path: Path | None = None, read_only: bool = False) -> duckdb.DuckDBPyConnection:
    p = path or db_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    ro = read_only and not one_mode()
    con = duckdb.connect(str(p), read_only=ro)
    if not ro:
        key = str(p.resolve())
        if not one_mode() or key not in _SCHEMA_APPLIED:
            apply_schema(con)
            _SCHEMA_APPLIED.add(key)
    return con


#: columns added after a table first shipped; `create table if not exists` will not add them to a live store
MIGRATIONS: tuple[tuple[str, str, str], ...] = (
    # DuckDB cannot add a constrained column to a live table, so migrations stay plain
    ("derived", "feature_spec", "is_count boolean"),
    ("read", "corpus_page", "source text"),
    # who asked (the key label, never the key): the API's roles, PRD §A.2
    ("agent", "conversation", "requested_by text"),
    ("agent", "conversation_turn", "requested_by text"),
)


def apply_schema(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(SCHEMA_SQL.read_text())
    for schema, table, column in MIGRATIONS:
        con.execute(f"alter table {schema}.{table} add column if not exists {column}")


def insert_frame(con: duckdb.DuckDBPyConnection, schema: str, table: str, df: Any, tier: str) -> int:
    """Create-or-replace `schema.table` from a DataFrame, stamping and checking the tier.

    The tier is not a hint: a frame destined for `native` cannot be written into `read`, and a frame that
    already carries a different tier column is refused rather than overwritten.
    """
    expected = TIER_BY_SCHEMA.get(schema)
    if expected is None:
        raise TierError(f"unknown schema {schema!r}")
    if tier != expected:
        raise TierError(f"refusing to write tier {tier!r} rows into schema {schema!r} (holds {expected!r})")
    if "tier" in getattr(df, "columns", []):
        present = {str(v) for v in df["tier"].dropna().unique()}
        if present - {tier}:
            raise TierError(f"{schema}.{table}: frame carries tier(s) {sorted(present)}, expected {tier!r}")
    else:
        df = df.copy()
        df["tier"] = tier
    con.execute(f"create schema if not exists {schema}")
    declared = con.execute(
        "select column_name, data_type from information_schema.columns where table_schema = ? and table_name = ? "
        "order by ordinal_position", [schema, table]).fetchall()
    if declared:
        # the table is declared in schema.sql: keep its types, defaults and constraints and replace the rows.
        # `create or replace ... as select` would retype every column from the frame (a column of nulls
        # becomes INT32) and drop the tier CHECK, which is how native.layer once lost its hash column.
        names = [c for c, _ in declared]
        extra = [c for c in df.columns if c not in names]
        if extra:
            raise TierError(f"{schema}.{table}: frame carries column(s) {extra} the schema does not declare")
        cols = [c for c in df.columns if c != "tier"]
        con.execute(f"delete from {schema}.{table}")
        con.register("ue_insert_frame", df)
        con.execute(f"insert into {schema}.{table} ({', '.join(cols)}, tier) "
                    f"select {', '.join(cols)}, '{tier}' from ue_insert_frame")
        con.unregister("ue_insert_frame")
        return len(df)
    con.register("ue_insert_frame", df)
    con.execute(f"create or replace table {schema}.{table} as select * from ue_insert_frame")
    con.unregister("ue_insert_frame")
    return len(df)


def append_frame(
    con: duckdb.DuckDBPyConnection, schema: str, table: str, df: Any, tier: str, replace: bool = False
) -> int:
    """Insert into a table declared in schema.sql, keeping its columns and its tier CHECK constraint.

    Unlike `insert_frame` this does not redefine the table, so the constraint stays in force: a row whose tier
    does not match the schema is rejected by DuckDB itself, not just by the guard here.
    """
    expected = TIER_BY_SCHEMA.get(schema)
    if expected is None:
        raise TierError(f"unknown schema {schema!r}")
    if tier != expected:
        raise TierError(f"refusing to write tier {tier!r} rows into schema {schema!r} (holds {expected!r})")
    if "tier" in getattr(df, "columns", []):
        present = {str(v) for v in df["tier"].dropna().unique()}
        if present - {tier}:
            raise TierError(f"{schema}.{table}: frame carries tier(s) {sorted(present)}, expected {tier!r}")
    if replace:
        con.execute(f"delete from {schema}.{table}")
    cols = [c for c in df.columns if c != "tier"]
    con.register("ue_append_frame", df)
    con.execute(
        f"insert into {schema}.{table} ({', '.join(cols)}, tier) "
        f"select {', '.join(cols)}, '{tier}' from ue_append_frame"
    )
    con.unregister("ue_append_frame")
    return len(df)


def write_meta(con: duckdb.DuckDBPyConnection, pipeline_version: str) -> None:
    con.execute("create or replace table meta as select ? as store_version, ? as pipeline_version, ? as built_at",
                [STORE_VERSION, pipeline_version, dt.datetime.now(dt.UTC).isoformat(timespec="seconds")])


def tier_audit(con: duckdb.DuckDBPyConnection) -> list[str]:
    """Every table in a tiered schema must carry a tier column holding only that schema's tier.

    Returns human-readable problems; an empty list means the store is clean. `ue store audit` prints these and
    the test suite asserts they are empty, so a new table cannot quietly skip the rule.
    """
    problems: list[str] = []
    rows = con.execute(
        "select table_schema, table_name from information_schema.tables "
        "where table_schema in ('native','read','derived','agent','expert') and table_type = 'BASE TABLE' "
        "order by 1, 2"
    ).fetchall()
    for schema, table in rows:
        cols = {c[0] for c in con.execute(f"describe {schema}.{table}").fetchall()}
        if "tier" not in cols:
            problems.append(f"{schema}.{table}: no tier column")
            continue
        found = {
            r[0] for r in con.execute(f"select distinct tier from {schema}.{table}").fetchall() if r[0] is not None
        }
        wrong = found - {TIER_BY_SCHEMA[schema]}
        if wrong:
            problems.append(f"{schema}.{table}: holds tier(s) {sorted(wrong)}, expected {TIER_BY_SCHEMA[schema]}")
        nulls = con.execute(f"select count(*) from {schema}.{table} where tier is null").fetchone()
        if nulls and nulls[0]:
            problems.append(f"{schema}.{table}: {nulls[0]} row(s) with a null tier")
    return problems


from .load import rebuild, stage_store  # noqa: E402  (public API; imported last to avoid a cycle)

__all__ = [
    "STORE_VERSION",
    "append_frame",
    "SCHEMA_SQL",
    "TIER_BY_SCHEMA",
    "TierError",
    "apply_schema",
    "connect",
    "db_path",
    "insert_frame",
    "rebuild",
    "stage_store",
    "tier_audit",
    "write_meta",
]
