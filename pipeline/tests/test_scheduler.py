"""The scheduler: caps, retries, the circuit breaker, and resuming after a usage limit (exit 75)."""

from __future__ import annotations

import json

import pytest

from uranium_explorer import extract as ex
from uranium_explorer.backends.base import (
    BackendConfigError,
    ExtractionRequest,
    ExtractionResponse,
    SchemaInvalidError,
    TransientBackendError,
    UsageLimitReached,
)
from uranium_explorer.backends.cache import CachedBackend

from fake_page import PDF_SHA, page_result


def config(**over) -> ex.Config:
    base = dict(id="test", model="claude-sonnet-5", effort="medium",
                page_classes=("assay_table", "lith_log", "collar_table"), max_pages_per_file=12,
                reask=False, validator_mode="enforce", max_budget_usd=0.6, timeout_s=60,
                class_priority=ex.CLASS_PRIORITY, note="")
    base.update(over)
    return ex.Config(**base)


def planned(page_no: int, tmp_path, chain_id=None, chain_pos=None, file_num="74H09-0039") -> ex.PlannedPage:
    img = tmp_path / f"p{page_no:04d}.png"
    img.write_bytes(b"\x89PNG" + bytes([page_no]) * 32)
    return ex.PlannedPage(
        page_id=f"pg:{PDF_SHA[:12]}:{page_no:04d}", file_num=file_num, pdf_sha256=PDF_SHA,
        pdf_name="x.pdf", page_no=page_no, image_path=str(img.relative_to(img.parents[1])) if False else str(img),
        image_sha256="0" * 64, route_class="assay_table", route_candidates=(), chain_id=chain_id,
        chain_pos=chain_pos, width_px=1700, height_px=2200, dpi=200)


class ScriptedBackend:
    """Answers per page number from a script: a result, or an exception to raise."""

    family = "claude_cli"

    def __init__(self, script: dict[int, object], default=None):
        self.script = script
        self.default = default if default is not None else page_result()
        self.calls: list[int] = []

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        page = int(req.images[0].stem[1:])
        self.calls.append(page)
        answer = self.script.get(page, self.default)
        if isinstance(answer, BaseException):
            raise answer
        if callable(answer):
            answer = answer(len([c for c in self.calls if c == page]))
            if isinstance(answer, BaseException):
                raise answer
        return ExtractionResponse(structured=answer, envelope={}, backend="scripted", backend_version="0",
                                  model_requested=req.model, model_resolved=req.model, num_turns=4,
                                  duration_s=0.01, usage={"output_tokens": 100,
                                                          "cache_creation_input_tokens": 7000,
                                                          "cache_read_input_tokens": 3000},
                                  cost_usd=0.05, cache_key=req.cache_key(self.family))


@pytest.fixture(autouse=True)
def isolate(tmp_path, monkeypatch):
    """Keep every scheduler test off the real data directories."""
    monkeypatch.setattr(ex, "read_results", lambda *a, **k: {})
    monkeypatch.setattr(ex, "write_results", lambda rows, path=None: tmp_path / "results.jsonl")
    monkeypatch.setattr(ex, "run_dir", lambda run_id: tmp_path / "runs" / run_id)
    monkeypatch.setattr(ex, "failed_path", lambda: tmp_path / "failed.jsonl")
    yield


def scheduler(backend, tmp_path, **over) -> ex.Scheduler:
    return ex.Scheduler(config(**over.pop("config", {})), backend, log=lambda *a: None,
                        max_calls=over.pop("max_calls", 10), launch_gap_s=0.0, **over)


def test_a_plain_run_calls_each_page_once_and_writes_the_log(tmp_path):
    backend = CachedBackend(ScriptedBackend({}), root=tmp_path / "cache")
    s = scheduler(backend, tmp_path)
    plan = [planned(n, tmp_path) for n in (4, 5, 6)]
    summary = s.run(plan)
    assert summary["done"] == 3 and summary["failed"] == 0 and summary["pending"] == []
    assert summary["totals"]["calls"] == 3
    calls = [json.loads(x) for x in (s.runlog.dir / "calls.jsonl").read_text().splitlines()]
    assert {c["status"] for c in calls} == {"ok"}
    assert all(c["n_rows"] == 7 for c in calls)
    assert json.loads((s.runlog.dir / "manifest.json").read_text())["planned"] == 3


def test_max_calls_is_a_hard_cap_and_the_rest_stay_pending(tmp_path):
    inner = ScriptedBackend({})
    s = scheduler(CachedBackend(inner, root=tmp_path / "cache"), tmp_path, max_calls=2)
    summary = s.run([planned(n, tmp_path) for n in (4, 5, 6, 7)])
    assert len(inner.calls) == 2
    assert summary["done"] == 2 and len(summary["pending"]) == 2
    assert summary["max_calls_reached"] is True
    assert json.loads((s.runlog.dir / "state.json").read_text())["reason"] == "max_calls"


def test_usage_limit_stops_launching_writes_state_and_resumes_from_cache(tmp_path):
    limit = UsageLimitReached("You have hit your usage limit · resets 3pm", "3pm")
    inner = ScriptedBackend({6: limit})
    s = scheduler(CachedBackend(inner, root=tmp_path / "cache"), tmp_path, max_calls=10,
                  workers=1)
    plan = [planned(n, tmp_path) for n in (4, 5, 6, 7, 8)]
    summary = s.run(plan)
    assert summary["usage_limit"]["resets_at_text"] == "3pm"
    assert summary["done"] == 2, "pages before the limit are kept"
    assert set(summary["pending"]) == {p.page_id for p in plan[2:]}
    state = json.loads((s.runlog.dir / "state.json").read_text())
    assert state["reason"] == "usage_limit" and state["resets_at_text"] == "3pm"
    assert len(state["pending_pages"]) == 3

    # resume: the two finished pages come from the cache, only the remaining three are called
    inner2 = ScriptedBackend({})
    s2 = scheduler(CachedBackend(inner2, root=tmp_path / "cache"), tmp_path, max_calls=10, workers=1)
    summary2 = s2.run(plan)
    assert summary2["done"] == 5 and summary2["pending"] == []
    assert sorted(inner2.calls) == [6, 7, 8], "the cached pages are never called again"
    assert summary2["totals"]["cached"] == 2


def test_two_transient_failures_are_retried_then_the_page_succeeds(tmp_path):
    attempts: dict[int, int] = {}

    def flaky(n):
        attempts[5] = n
        return TransientBackendError("overloaded") if n < 3 else page_result()

    s = scheduler(CachedBackend(ScriptedBackend({5: flaky}), root=tmp_path / "cache"), tmp_path,
                  retry_delays_s=(0.0, 0.0))
    summary = s.run([planned(5, tmp_path)])
    assert attempts[5] == 3
    assert summary["done"] == 1 and summary["failed"] == 0


def test_a_third_transient_failure_gives_up_without_retrying_forever(tmp_path):
    inner = ScriptedBackend({5: TransientBackendError("overloaded")})
    s = scheduler(CachedBackend(inner, root=tmp_path / "cache"), tmp_path, retry_delays_s=(0.0, 0.0))
    summary = s.run([planned(5, tmp_path)])
    assert len(inner.calls) == 3, "one attempt plus two retries"
    assert summary["failed"] == 1 and summary["pending"] == [f"pg:{PDF_SHA[:12]}:0005"]


def test_a_schema_invalid_answer_is_retried_once(tmp_path):
    seen: dict[int, int] = {}

    def bad_then_good(n):
        seen[5] = n
        return {"nonsense": True} if n == 1 else page_result()

    inner = ScriptedBackend({5: bad_then_good})
    backend = CachedBackend(inner, root=tmp_path / "cache")
    s = scheduler(backend, tmp_path, retry_delays_s=(0.0, 0.0))
    req = s.request_for(planned(5, tmp_path), [])
    summary = s.run([planned(5, tmp_path)])
    assert seen[5] == 2, "the invalid answer is dropped from the cache so the retry really re-asks"
    assert summary["done"] == 1
    # the unusable answer left a failure record and the usable one replaced it in the cache
    from uranium_explorer.backends.cache import failure_path
    assert failure_path(req.cache_key("claude_cli"), tmp_path / "cache").is_file()
    assert backend.cached(req).structured["tables"]


def test_a_budget_or_turn_error_is_never_retried(tmp_path):
    for error, expected_calls in ((TransientBackendError("exceeded max budget of $0.60"), 1),
                                  (BackendConfigError("model answered without a tool turn"), 1)):
        ex.failed_path().unlink(missing_ok=True)   # each error type starts with an empty give-up list
        inner = ScriptedBackend({5: error})
        s = scheduler(CachedBackend(inner, root=tmp_path / "cache"), tmp_path, retry_delays_s=(0.0, 0.0))
        s.run([planned(5, tmp_path)])
        assert len(inner.calls) == expected_calls, error


def test_the_circuit_breaker_stops_after_three_unknown_failures(tmp_path):
    inner = ScriptedBackend({n: BackendConfigError("permission denied") for n in range(4, 12)})
    s = scheduler(CachedBackend(inner, root=tmp_path / "cache"), tmp_path, max_calls=20, workers=1,
                  retry_delays_s=(0.0, 0.0))
    summary = s.run([planned(n, tmp_path) for n in range(4, 12)])
    assert summary["circuit_broken"] is True
    assert summary["failed"] == ex.CIRCUIT_BREAK_AFTER
    # the three failed pages stay pending too: a resume retries them rather than declaring them read
    assert len(summary["pending"]) == 8


def test_a_continuation_page_carries_the_previous_page_and_changes_the_cache_key(tmp_path):
    inner = ScriptedBackend({})
    s = scheduler(CachedBackend(inner, root=tmp_path / "cache"), tmp_path, workers=1)
    chain = [planned(4, tmp_path, chain_id="c1", chain_pos=1),
             planned(5, tmp_path, chain_id="c1", chain_pos=2)]
    s.run(chain)
    keys = [r["cache_key"] for r in s.results.values()]
    assert len(set(keys)) == 2
    carry = ex.carry_from_result(page_result(), 4)
    assert any(c.label == "hole identifier" and c.value == "R-78-27" for c in carry)
    assert any(c.label == "column headers" for c in carry)
    assert ex.context_hash(carry) and ex.context_hash([]) == ""


def test_the_launch_gap_is_honoured(tmp_path):
    import time

    inner = ScriptedBackend({})
    s = ex.Scheduler(config(), CachedBackend(inner, root=tmp_path / "cache"), log=lambda *a: None,
                     max_calls=3, launch_gap_s=0.2, workers=2)
    t0 = time.monotonic()
    s.run([planned(n, tmp_path) for n in (4, 5, 6)])
    assert time.monotonic() - t0 >= 0.4, "three launches need at least two gaps"


def test_reask_asks_again_when_a_table_is_short(tmp_path):
    short = page_result(truncate_rows=2, printed_row_count=5)
    inner = ScriptedBackend({5: lambda n: short if n == 1 else page_result()})
    s = scheduler(CachedBackend(inner, root=tmp_path / "cache"), tmp_path,
                  config={"reask": True}, retry_delays_s=(0.0, 0.0))
    summary = s.run([planned(5, tmp_path)])
    assert len(inner.calls) == 2, "one re-ask when the counted rows exceed the returned rows"
    assert summary["done"] == 1
    note = ex.Scheduler._reask_note(short)
    assert "5 printed" in note and "returned 3" in note


def test_the_plan_never_includes_a_held_out_file():
    rows = [
        {"page_id": "a", "file_num": "74H06-0039", "pdf_sha256": "a" * 64, "pdf_name": "x", "page_no": 1,
         "image_path": "p", "image_sha256": "b" * 64, "route_class": "assay_table", "route_candidates": "",
         "chain_id": None, "chain_pos": None, "width_px": 1, "height_px": 1, "dpi": 200,
         "rendered": True, "large_format": False},
        {"page_id": "b", "file_num": "74H09-0039", "pdf_sha256": "c" * 64, "pdf_name": "x", "page_no": 1,
         "image_path": "p", "image_sha256": "d" * 64, "route_class": "assay_table", "route_candidates": "",
         "chain_id": None, "chain_pos": None, "width_px": 1, "height_px": 1, "dpi": 200,
         "rendered": True, "large_format": False},
    ]
    plan = ex.build_plan(config(), split="dev", rows=rows)
    assert [p.file_num for p in plan] == ["74H09-0039"]
    assert "74H06-0039" in ex.heldout_files()
    held = ex.build_plan(config(), split="heldout", rows=rows)
    assert [p.file_num for p in held] == ["74H06-0039"]


def test_heldout_needs_an_unlock():
    with pytest.raises(PermissionError, match="unlock-heldout"):
        ex.stage_extract(split="heldout", dry_run=True, log=lambda *a: None)


def test_the_held_out_lock_still_matches_what_is_on_disk():
    assert ex.check_heldout_lock() == []


def test_the_estimate_comes_from_recorded_usage(tmp_path):
    est = ex.estimate_cost(48, cache_root=tmp_path / "empty")
    assert est["samples"] >= 1
    assert est["total_cost_usd"] > 0
    assert est["total_tokens"]["output"] > 0


def test_results_are_written_after_every_page_not_only_at_the_end(tmp_path, monkeypatch):
    """A run killed part-way keeps every page it paid for: the results file is rewritten after each page."""
    writes: list[dict] = []
    monkeypatch.setattr(ex, "write_results", lambda rows, path=None: writes.append(dict(rows)) or (tmp_path / "r.jsonl"))
    monkeypatch.setattr(ex, "read_results", lambda *a, **k: {"pg:old": {"page_id": "pg:old", "result": {}}})
    s = scheduler(CachedBackend(ScriptedBackend({}), root=tmp_path / "cache"), tmp_path, workers=1)
    s.run([planned(n, tmp_path) for n in (4, 5, 6)])
    assert len(writes) >= 4                                   # three pages plus the final write
    assert "pg:old" in writes[0] and len(writes[0]) == 2       # the prior rows travel with the first page
    assert len(writes[-1]) == 4


def test_a_page_that_failed_every_attempt_is_skipped_next_run_unless_asked_for(tmp_path):
    """The give-up list: a page the model cannot finish costs its attempts once, not once per resume."""
    inner = ScriptedBackend({5: lambda n: TransientBackendError("cannot finish")})
    s = scheduler(CachedBackend(inner, root=tmp_path / "cache"), tmp_path, retry_delays_s=(0.0, 0.0))
    first = s.run([planned(n, tmp_path) for n in (4, 5)])
    assert first["done"] == 1 and first["failed"] == 1
    assert [r["page_no"] for r in ex.read_failed().values()] == [5]
    # the next run plans page 5 again but skips it, and says so
    inner2 = ScriptedBackend({})
    s2 = scheduler(CachedBackend(inner2, root=tmp_path / "cache2"), tmp_path)
    second = s2.run([planned(n, tmp_path) for n in (4, 5)])
    assert second["skipped_failed"] == [planned(5, tmp_path).page_id] and second["done"] == 1
    assert inner2.calls == [4]
    # asked for, it is tried again
    s3 = scheduler(CachedBackend(ScriptedBackend({}), root=tmp_path / "cache3"), tmp_path, retry_failed=True)
    third = s3.run([planned(n, tmp_path) for n in (5,)])
    assert third["done"] == 1 and third["skipped_failed"] == []
