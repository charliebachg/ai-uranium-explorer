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


def test_store_sha_hashes_the_file_and_is_none_without_one(store: Path, tmp_path: Path) -> None:
    assert SN.store_sha(store) == SN._sha256(store)
    assert SN.store_sha(tmp_path / "missing.duckdb") is None


def test_latest_is_the_most_recently_taken_manifest_or_none(store: Path) -> None:
    assert SN.latest() is None
    for stamp, sha in (("2026-02-01T00:00:00+00:00", "b" * 64), ("2026-01-01T00:00:00+00:00", "a" * 64)):
        (SN.snapshots_dir() / f"{stamp.replace(':', '')}-{sha[:12]}.json").write_text(
            json.dumps({"version": SN.SNAPSHOT_VERSION, "taken_at": stamp, "store_sha256": sha}))
    assert SN.latest()["store_sha256"] == "b" * 64, "the later manifest, whatever the file order"
    m = SN.take(log=lambda *a: None, path=store)
    assert SN.latest()["store_sha256"] == m["store_sha256"]


def test_pin_holds_a_named_snapshot_and_refuses_a_wrong_or_changed_one(store: Path) -> None:
    sha = SN.take(log=lambda *a: None, path=store)["store_sha256"]
    lines: list[str] = []
    assert SN.pin(sha[:12], path=store, log=lines.append) == {"store_sha256": sha, "snapshot": sha[:12], "pinned": True}
    assert lines == [f"  store {sha[:12]} (snapshot {sha[:12]}, pinned)"]
    with pytest.raises(FileNotFoundError):
        SN.pin("nope", path=store, log=lambda *a: None)
    con = connect(store)
    con.execute("create or replace table meta as select 'store/v2' as store_version, '0.2.0' as pipeline_version, 't' as built_at")
    con.close()
    with pytest.raises(RuntimeError, match="not snapshot"):
        SN.pin(sha[:12], path=store, log=lambda *a: None)


def test_pin_without_a_hash_names_the_matching_snapshot_or_says_none_does(store: Path, tmp_path: Path) -> None:
    sha = SN._sha256(store)
    lines: list[str] = []
    assert SN.pin(None, path=store, log=lines.append) == {"store_sha256": sha, "snapshot": None, "pinned": False}
    assert lines[-1] == f"  store {sha[:12]} (no snapshot names it)"
    SN.take(log=lambda *a: None, path=store)
    assert SN.pin(None, path=store, log=lines.append) == {"store_sha256": sha, "snapshot": sha[:12], "pinned": False}
    assert lines[-1] == f"  store {sha[:12]} (snapshot {sha[:12]})"
    assert SN.pin(None, path=tmp_path / "missing.duckdb", log=lambda *a: None) == {"store_sha256": None, "snapshot": None, "pinned": False}


def test_lineage_on_the_real_store_names_every_break_or_none() -> None:
    """Runs against the analytics store as it is; the assertion is that the check itself is sound."""
    problems = SN.lineage(log=lambda *a: None)
    assert isinstance(problems, list)
    for p in problems:
        assert ":" in p, p


def test_a_snapshot_records_feature_quantiles_and_drift_compares_them(store: Path) -> None:
    con = connect(store)
    ins = ("insert into derived.cell_feature (cell_id, feature_key, value, n_obs, nearest_m, from_tier, op, tool, computed_at) values ")
    con.execute(ins + "('a', 'd_fault_m', 100, 1, 100, 'native', 'distance', 'lr', 't'), ('b', 'd_fault_m', 200, 1, 200, 'native', 'distance', 'lr', 't'), "
                "('c', 'd_fault_m', 300, 1, 300, 'native', 'distance', 'lr', 't'), ('a', 'flat', 1, 1, null, 'native', 'count', 'lr', 't')")
    con.close()
    m = SN.take(log=lambda *a: None, path=store)
    assert m["features"]["d_fault_m"]["n"] == 3 and m["features"]["d_fault_m"]["q"][2] == 200.0
    # nothing changed: nothing drifts
    out = SN.drift(m["store_sha256"][:12], path=store, log=lambda *a: None)
    assert out["drifted"] == []
    # the median moves by more than a quarter of the interquartile range, and a feature appears
    con = connect(store)
    con.execute("update derived.cell_feature set value = value + 80 where feature_key = 'd_fault_m'")
    con.execute(ins + "('a', 'new_one', 5, 1, null, 'native', 'count', 'lr', 't')")
    con.close()
    out = SN.drift(m["store_sha256"][:12], path=store, log=lambda *a: None)
    assert set(out["drifted"]) == {"d_fault_m", "new_one"}
    row = next(r for r in out["rows"] if r["feature_key"] == "d_fault_m")
    assert row["median_shift_iqr"] == 0.8 and "median moved" in row["why"]


def test_compare_features_is_pure_and_names_each_reason() -> None:
    before = {"x": {"n": 100, "with_obs": 100, "q": [0, 1, 2, 3, 4]}, "gone": {"n": 1, "with_obs": 1, "q": [1, 1, 1, 1, 1]}}
    now = {"x": {"n": 60, "with_obs": 60, "q": [0, 1, 2, 3, 4]}}
    rows = {r["feature_key"]: r for r in SN.compare_features(before, now)}
    assert rows["x"]["drifted"] and rows["x"]["why"] == "cells with a value changed -40%"
    assert rows["gone"]["why"] == "only in the snapshot"
