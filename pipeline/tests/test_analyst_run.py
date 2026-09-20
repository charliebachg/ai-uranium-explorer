"""The harness over a six-cell benchmark: open cells run, held-out cells are refused, finished cells are
carried forward on resume, the budget stops it cleanly, and the run names itself."""

from __future__ import annotations

import json

import pytest

from legacy_reader.analyst import arms as A
from legacy_reader.analyst import run as RUN
from legacy_reader.analyst import score as SC
from legacy_reader.backends.base import TransientBackendError, UsageLimitReached

from fake_bench import (LABELLED, OPEN, AnalystBackend, BudgetExhausted, FakeManifest, bad_answer, install_runtime,
                        make_bench)


@pytest.fixture
def world(tmp_path, monkeypatch):
    rt = install_runtime(monkeypatch, tmp_path)
    bench = make_bench(tmp_path)
    return rt, bench


def run(bench, backend, rt, **over):
    kw = dict(budget_usd=80.0, log=lambda *_: None, workers=1, track=True, cache_root=rt.cache, boot=20)
    kw.update(over)
    arm = kw.pop("arm", None) or A.load_arm("v0")
    return RUN.run_arm(bench.version, arm, lambda _arm: backend, **kw)


def rows(rt, run_id):
    return SC.read_cells(rt.runs / run_id)


def test_a_run_covers_the_open_cells_and_never_the_held_out_one(world) -> None:
    rt, bench = world
    backend = AnalystBackend()
    s = run(bench, backend, rt)
    assert sorted(backend.calls) == OPEN, "b06 is held out and never called"
    assert s["done"] == 5 and s["failed"] == 0 and s["pending"] == []
    got = rows(rt, s["run_id"])
    assert set(got) == set(OPEN)
    row = got["b01"]
    assert row["published"] is True and row["cell_id"] == "0001_0001" and row["stratum"] == "deposit"
    assert row["arm"] == "v0" and row["run_id"] == s["run_id"] and row["fold"] == 0
    assert s["spent_usd"] == pytest.approx(0.25)
    # the run names itself: manifest before the first call, and again with the spend
    m = FakeManifest.started[-1]
    assert m.kind == "bench" and m.bench == {"bench_id": bench.version, "manifest_sha256": bench.manifest_sha256}
    assert m.models == {"analyst": "claude-opus-5"} and m.config["switches"]["oof_scores"] is False
    assert m.budget_usd == 80.0 and m.spent_usd == pytest.approx(0.25) and m.writes == 2
    assert set(m.prompt_hashes) == {"analyst/v0/v1"} and set(m.schema_hashes) == {"1.0.0"} and m.scores_seen == []
    written = json.loads((rt.runs / s["run_id"] / "manifest.json").read_text())
    assert written["spent_usd"] == pytest.approx(0.25)
    # scored, tracked, and pointed at for the table
    assert s["score"]["n"] == 4 and s["score"]["n_probe"] == 1 and s["score"]["f1"] == pytest.approx(2 * 3 / (2 * 3 + 1))
    assert s["mlflow_run_id"] == "mlflow-1"
    logged = rt.logged[0]
    assert logged["tags"]["kind"] == "bench" and logged["params"]["bench_manifest_sha256"] == bench.manifest_sha256
    assert logged["params"]["arm"] == "v0" and "f1" in logged["metrics"]
    pointer = json.loads((SC.out_dir(bench.version) / "arms" / "v0.json").read_text())
    assert pointer["run_id"] == s["run_id"] and pointer["mlflow_run_id"] == "mlflow-1"
    assert (rt.runs / s["run_id"] / "score.json").is_file() and (rt.runs / s["run_id"] / "summary.json").is_file()


def test_naming_a_held_out_cell_is_refused_before_anything_runs(world) -> None:
    rt, bench = world
    backend = AnalystBackend()
    with pytest.raises(PermissionError, match="b06"):
        run(bench, backend, rt, cells=["b01", "b06"])
    assert backend.calls == [] and FakeManifest.started == []


def test_the_oof_switch_is_recorded_on_the_manifest(world) -> None:
    rt, bench = world
    run(bench, AnalystBackend(), rt, arm=A.load_arm("v0-scores"), cells=["b01"])
    assert FakeManifest.started[-1].scores_seen == [{"model_version": m, "fold_kind": "spatial"} for m in ("learned", "effort", "criteria")]


def test_a_resumed_run_carries_finished_cells_forward_and_calls_only_the_rest(world, tmp_path) -> None:
    rt, bench = world
    first = run(bench, AnalystBackend(), rt, cells=["b01", "b02"])
    assert first["done"] == 2
    again = AnalystBackend()
    second = run(bench, again, rt, resume=first["run_id"], cache_root=tmp_path / "other-cache")
    assert sorted(again.calls) == ["b03", "b04", "b05"], "a different cache root: only skipping explains it"
    assert second["carried"] == 2 and second["done"] == 3 and second["resumed_from"] == first["run_id"]
    got = rows(rt, second["run_id"])
    assert set(got) == set(OPEN)
    assert got["b01"]["resumed_from"] == first["run_id"] and "resumed_from" not in got["b03"]
    assert second["score"]["n"] == 4


def test_the_budget_stops_the_run_cleanly_with_the_rest_pending(world) -> None:
    rt, bench = world
    backend = AnalystBackend(script={"b03": BudgetExhausted("the next call would cross the ceiling")})
    s = run(bench, backend, rt)
    assert backend.calls == ["b01", "b02", "b03"]
    assert s["done"] == 2 and s["pending"] == ["b03", "b04", "b05"] and s["budget_exhausted"] is True
    assert s["usage_limited"] is False and set(rows(rt, s["run_id"])) == {"b01", "b02"}
    assert FakeManifest.started[-1].spent_usd == pytest.approx(0.10)
    assert s["score"]["n"] == 2, "what finished is still scored"


def test_the_run_budget_refuses_a_call_that_could_cross_it(world) -> None:
    """The real budget: the arm's per-call ceiling ($1.50) is the estimate, so with $1.55 the third call, which
    could take the run to $1.60, is refused before it is sent."""
    rt, bench = world
    backend = AnalystBackend(cost=0.05)
    s = run(bench, backend, rt, budget_usd=1.55)
    assert backend.calls == ["b01", "b02"]
    assert s["pending"] == ["b03", "b04", "b05"] and s["budget_exhausted"] is True and s["spent_usd"] == pytest.approx(0.10)
    assert FakeManifest.started[-1].spent_usd == pytest.approx(0.10)


def test_a_cache_hit_costs_the_budget_nothing(world) -> None:
    rt, bench = world
    first = run(bench, AnalystBackend(), rt, cells=["b01"])
    again = AnalystBackend()
    second = run(bench, again, rt, cells=["b01"])
    assert again.calls == [] and second["done"] == 1 and second["spent_usd"] == 0.0
    assert rows(rt, second["run_id"])["b01"]["from_cache"] is True and first["spent_usd"] == pytest.approx(0.05)


def test_an_arm_the_benchmark_was_not_built_for_is_refused(world) -> None:
    rt, bench = world
    backend = AnalystBackend()
    with pytest.raises(ValueError, match="drillholes"):
        run(bench, backend, rt, arm=A.load_arm("v0-holes"))
    assert backend.calls == [] and FakeManifest.started == []


def test_the_real_manifest_runs_the_harness_too(world, monkeypatch) -> None:
    """The fake mirrors `runtime.manifest.Manifest`; this proves the mirror is faithful."""
    from legacy_reader.runtime.manifest import Manifest

    rt, bench = world
    monkeypatch.setattr(RUN, "Manifest", Manifest)
    s = run(bench, AnalystBackend(), rt, cells=["b01", "b04"])
    written = Manifest.read(rt.runs / s["run_id"])
    assert written.kind == "bench" and written.run_id.endswith("-bench") and written.spent_usd == pytest.approx(0.10)
    assert written.bench == {"bench_id": bench.version, "manifest_sha256": bench.manifest_sha256}
    assert set(written.prompt_hashes) == {"analyst/v0/v1"} and written.finished_at


def test_a_usage_limit_stops_the_run_the_same_way(world) -> None:
    rt, bench = world
    backend = AnalystBackend(script={"b02": UsageLimitReached("hit your usage limit · resets 3pm", "3pm")})
    s = run(bench, backend, rt)
    assert s["done"] == 1 and s["usage_limited"] is True and s["resets_at_text"] == "3pm"
    assert s["pending"] == ["b02", "b03", "b04", "b05"]


def test_a_rejected_answer_is_written_as_rejected_and_a_backend_error_as_failed(world) -> None:
    rt, bench = world
    backend = AnalystBackend(script={"b01": bad_answer("b01"),
                                                   "b04": TransientBackendError("claude timed out")})
    s = run(bench, backend, rt)
    got = rows(rt, s["run_id"])
    assert got["b01"]["published"] is False and any("2.4" in p for p in got["b01"]["problems"])
    assert got["b04"]["answer"] is None and "timed out" in got["b04"]["error"] and got["b04"]["cost_usd"] == 0.0
    assert s["done"] == 4 and s["failed"] == 1
    assert s["score"]["n_rejected"] == 1 and s["score"]["n_failed"] == 1 and s["score"]["gate_rejection_rate"] == 0.25
    # a failed cell is not "done": a resume runs it again, and does not run the rejected one again
    again = AnalystBackend()
    second = run(bench, again, rt, resume=s["run_id"])
    assert again.calls == ["b04"]


def test_cells_are_appended_as_they_finish_not_at_the_end(world) -> None:
    rt, bench = world
    seen = []

    class Watching(AnalystBackend):
        def call(self, req):
            run_dirs = list(rt.runs.glob("bench-*")) if rt.runs.is_dir() else []
            seen.append(len(SC.read_cells(run_dirs[0])) if run_dirs else -1)
            return super().call(req)

    run(bench, Watching(), rt)
    assert seen == [0, 1, 2, 3, 4], "each call sees every earlier cell already on disk"


def test_a_drifted_benchmark_is_refused(world) -> None:
    rt, bench = world
    (bench.dir / "packs" / "b01.json").write_text("{}")
    with pytest.raises(ValueError, match="drifted"):
        run(bench, AnalystBackend(), rt)


def test_workers_run_in_parallel_and_every_cell_still_lands(world) -> None:
    rt, bench = world
    backend = AnalystBackend()
    s = run(bench, backend, rt, workers=3)
    assert s["done"] == 5 and sorted(backend.calls) == OPEN and set(rows(rt, s["run_id"])) == set(OPEN)
    assert sorted(LABELLED) == sorted(b for b in rows(rt, s["run_id"]) if b != "b05")
