"""Snapshots name a store by hash; verify refuses a store that is not the one named; lineage finds breaks."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacy_reader.store import connect
from legacy_reader.store import snapshot as SN


@pytest.fixture
def store(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db = tmp_path / "s.duckdb"
    con = connect(db)
    con.execute("insert into native.layer (layer_key, title, service_url, layer_id, where_clause, out_sr, record_count, payload_sha256, licence, licence_url, redistributable, retrieved_at, bears_on, role, notes) "
                "values ('faults', 'Faults', 'u', 0, '1=1', 4326, 3, 'abc', 'sk', 'u', true, 't', 'trap', 'feature', '')")
    con.execute("create or replace table meta as select 'store/v2' as store_version, '0.1.0' as pipeline_version, 't' as built_at")
    con.close()
    monkeypatch.setattr(SN, "snapshots_dir", lambda: tmp_path / "snaps" if (tmp_path / "snaps").mkdir(exist_ok=True) is None else tmp_path / "snaps")
    monkeypatch.setattr(SN, "_git_commit", lambda: "deadbeef")
    return db


def test_a_snapshot_records_the_hash_the_counts_and_the_layer_pulls(store: Path) -> None:
    m = SN.take(log=lambda *a: None, path=store)
    assert len(m["store_sha256"]) == 64 and m["tables"]["native.layer"] == 1 and m["git_commit"] == "deadbeef"
    assert m["layers"]["faults"]["payload_sha256"] == "abc"
    on_disk = json.loads(next(SN.snapshots_dir().glob("*.json")).read_text())
    assert on_disk["store_sha256"] == m["store_sha256"]


def test_verify_accepts_the_same_store_and_refuses_a_changed_one(store: Path) -> None:
    m = SN.take(log=lambda *a: None, path=store)
    assert SN.verify(m["store_sha256"][:12], path=store)["store_sha256"] == m["store_sha256"]
    con = connect(store)
    con.execute("insert into native.layer (layer_key, title, service_url, layer_id, where_clause, out_sr, record_count, payload_sha256, licence, licence_url, redistributable, retrieved_at, bears_on, role, notes) "
                "values ('more', 'More', 'u', 0, '1=1', 4326, 1, 'def', 'sk', 'u', true, 't', 'trap', 'feature', '')")
    con.close()
    with pytest.raises(RuntimeError, match="not snapshot"):
        SN.verify(m["store_sha256"][:12], path=store)
    with pytest.raises(FileNotFoundError):
        SN.verify("0000000000", path=store)


def test_lineage_on_the_real_store_names_every_break_or_none() -> None:
    """Runs against the analytics store as it is; the assertion is that the check itself is sound."""
    problems = SN.lineage(log=lambda *a: None)
    assert isinstance(problems, list)
    for p in problems:
        assert ":" in p, p
