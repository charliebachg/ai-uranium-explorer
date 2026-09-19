"""A frame written into a declared table keeps the declared types and the tier constraint."""

from __future__ import annotations

from pathlib import Path

import pandas as pd
import pytest

from legacy_reader.store import TierError, connect, insert_frame


def _layer_frame(**over) -> pd.DataFrame:
    row = {"layer_key": "faults", "title": "Faults", "service_url": "u", "layer_id": None, "where_clause": None,
           "out_sr": 4326, "record_count": 3, "payload_sha256": None, "licence": "sk", "licence_url": None,
           "redistributable": True, "retrieved_at": "t", "bears_on": None, "role": "feature", "notes": None}
    row.update(over)
    return pd.DataFrame([row])


def test_a_declared_table_keeps_its_types_when_the_frame_is_all_nulls(tmp_path: Path) -> None:
    con = connect(tmp_path / "s.duckdb")
    try:
        insert_frame(con, "native", "layer", _layer_frame(), "native")
        types = dict(con.execute("select column_name, data_type from information_schema.columns "
                                 "where table_schema = 'native' and table_name = 'layer'").fetchall())
        assert types["payload_sha256"] == "VARCHAR" and types["bears_on"] == "VARCHAR"
        # the constraint survived too: a row of the wrong tier is refused by the database itself
        with pytest.raises(Exception, match="(?i)constraint|check"):
            con.execute("insert into native.layer (layer_key, title, service_url, record_count, licence, redistributable, "
                        "retrieved_at, role, tier) values ('x', 'x', 'u', 1, 'sk', true, 't', 'feature', 'read')")
        con.execute("update native.layer set payload_sha256 = 'abc' where layer_key = 'faults'")
        assert con.execute("select payload_sha256 from native.layer").fetchone()[0] == "abc"
    finally:
        con.close()


def test_a_second_insert_replaces_the_rows_rather_than_appending(tmp_path: Path) -> None:
    con = connect(tmp_path / "s.duckdb")
    try:
        insert_frame(con, "native", "layer", _layer_frame(), "native")
        insert_frame(con, "native", "layer", _layer_frame(layer_key="conductors", title="Conductors"), "native")
        assert con.execute("select layer_key from native.layer").fetchall() == [("conductors",)]
    finally:
        con.close()


def test_a_frame_column_the_schema_does_not_declare_is_refused(tmp_path: Path) -> None:
    con = connect(tmp_path / "s.duckdb")
    try:
        with pytest.raises(TierError, match="does not declare"):
            insert_frame(con, "native", "layer", _layer_frame(surprise=1), "native")
    finally:
        con.close()


def test_an_undeclared_table_is_still_created_from_the_frame(tmp_path: Path) -> None:
    con = connect(tmp_path / "s.duckdb")
    try:
        n = insert_frame(con, "read", "report", pd.DataFrame([{"file_num": "A", "holes": 2}]), "read")
        assert n == 1 and con.execute("select file_num, tier from read.report").fetchall() == [("A", "read")]
    finally:
        con.close()
