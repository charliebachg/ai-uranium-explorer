"""The run manifest: one shape for every kind of run, naming the store, the commit and the bill."""

from __future__ import annotations

import json
from pathlib import Path

from uranium_explorer.ids import sha256_file
from uranium_explorer.runtime.manifest import Manifest
from uranium_explorer.runtime.runs import new_run_id, run_dir
from uranium_explorer.runtime.spend import RunBudget
from uranium_explorer.store import snapshot as SN


def test_a_manifest_round_trips_through_its_file(tmp_path: Path) -> None:
    m = Manifest(run_id="20260920T120000Z-memo", kind="memo", started_at="2026-09-20T12:00:00+00:00",
                 config={"cell_id": "0123_0045", "mode": "panel"}, models={"proponent": "claude-sonnet-5"},
                 prompt_hashes={"system": "a" * 64}, schema_hashes={"step": "b" * 64}, store_sha256="c" * 64,
                 snapshot="c" * 12, git_commit="deadbeef", bench={"bench_id": "ub-v1", "manifest_sha256": "d" * 64},
                 scores_seen=[{"model_version": 3, "fold": 2}], blind_list_sha256="e" * 64, seed=7,
                 budget_usd=2.0, spent_usd=0.0, notes="a test")
    path = m.write(tmp_path / "run")
    assert path == tmp_path / "run" / "manifest.json"
    assert Manifest.read(tmp_path / "run") == m
    raw = json.loads(path.read_text())
    assert set(raw) == {"run_id", "kind", "started_at", "finished_at", "config", "models", "prompt_hashes",
                        "schema_hashes", "store_sha256", "snapshot", "git_commit", "bench", "scores_seen",
                        "blind_list_sha256", "seed", "budget_usd", "spent_usd", "notes"}


def test_start_names_the_store_the_snapshot_and_the_commit(prospect_sandbox) -> None:
    db = prospect_sandbox.make_store()
    m = Manifest.start("memo", {"cell_id": "x"}, {"proponent": "claude-sonnet-5"}, seed=1, budget_usd=1.5,
                       bench={"bench_id": "ub-v1", "manifest_sha256": "d" * 64})
    assert m.run_id.endswith("-memo") and m.kind == "memo" and m.started_at
    assert m.store_sha256 == sha256_file(db)
    assert m.snapshot is None, "no snapshot has been taken of this store, so the run names none"
    assert m.git_commit == "deadbeef"
    assert m.bench == {"bench_id": "ub-v1", "manifest_sha256": "d" * 64} and m.seed == 1 and m.budget_usd == 1.5
    assert m.finished_at is None and m.spent_usd == 0.0

    SN.take(log=lambda *a: None, path=db)
    assert Manifest.start("memo", {}, {}).snapshot == m.store_sha256[:12], "a taken snapshot is named"


def test_start_without_a_store_records_none_rather_than_failing(prospect_sandbox) -> None:
    m = Manifest.start("chat", {}, {"chat": "gpt-5-mini"})
    assert m.store_sha256 is None and m.snapshot is None
    assert m.models == {"chat": "gpt-5-mini"}


def test_finish_and_budget(tmp_path: Path) -> None:
    m = Manifest.start("bench", {}, {}, budget_usd=1.5)
    assert m.budget() == RunBudget(cap_usd=1.5, spent_usd=0.0)
    m.finish(0.25)
    assert m.finished_at is not None and m.spent_usd == 0.25
    assert m.budget().remaining() == 1.25
    assert m.write(tmp_path / "b") == tmp_path / "b" / "manifest.json"
    assert Manifest.read(tmp_path / "b").spent_usd == 0.25


def test_read_tolerates_keys_it_does_not_know(tmp_path: Path) -> None:
    (tmp_path / "manifest.json").write_text(json.dumps(
        {"run_id": "r", "kind": "other", "started_at": "t", "planned": 3, "totals": {"calls": 3}}))
    m = Manifest.read(tmp_path)
    assert m.run_id == "r" and m.kind == "other" and m.config == {} and m.spent_usd == 0.0


def test_run_ids_and_directories() -> None:
    from uranium_explorer.paths import PATHS

    rid = new_run_id("bench")
    assert rid.endswith("-bench") and rid[8] == "T" and rid[15] == "Z"
    assert run_dir(rid) == PATHS.runs / rid


def test_two_runs_claimed_in_the_same_second_get_two_directories(tmp_path, monkeypatch) -> None:
    from uranium_explorer.runtime import runs as R

    monkeypatch.setattr(R, "run_dir", lambda run_id: tmp_path / run_id)
    monkeypatch.setattr(R, "new_run_id", lambda kind: f"20260920T000000Z-{kind}")
    a, pa = R.claim_run_dir("bench-v0-text")
    b, pb = R.claim_run_dir("bench-v0-text")
    c, pc = R.claim_run_dir("bench-v0-features")
    assert a != b and pa != pb and pa.is_dir() and pb.is_dir()
    assert b.endswith("-2") and c == "20260920T000000Z-bench-v0-features"
