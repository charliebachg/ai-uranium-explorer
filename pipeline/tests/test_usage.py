"""The usage summary reads a run's own records and flags a call that resolved to the wrong model."""

from __future__ import annotations

import json
from pathlib import Path

from uranium_explorer.usage import format_report, summarise_run


def run_dir(tmp_path: Path, model_second: str = "claude-opus-5") -> Path:
    d = tmp_path / "20260919T000000Z-opus1"
    d.mkdir()
    (d / "manifest.json").write_text(json.dumps({"config": {"id": "opus1", "model": "claude-opus-5"}}))
    events = [
        {"at": "2026-09-19T00:00:00+00:00", "event": "plan", "n_pages": 2, "files": ["MAW00509"]},
        {"at": "2026-09-19T00:00:01+00:00", "event": "launch", "page_no": 3},
    ]
    calls = [
        {"at": "2026-09-19T00:01:00+00:00", "status": "ok", "file_num": "MAW00509", "page_no": 3, "attempt": 1,
         "model_resolved": "claude-opus-5", "num_turns": 4, "duration_s": 59.0, "cost_usd": 0.5,
         "tokens": {"input": 10, "cache_creation": 4000, "cache_read": 15000, "output": 9000}, "n_tables": 1,
         "n_rows": 12, "page_kind": "collar_table"},
        {"at": "2026-09-19T00:02:30+00:00", "status": "ok", "file_num": "MAW00509", "page_no": 5, "attempt": 1,
         "model_resolved": model_second, "num_turns": 3, "duration_s": 41.0, "cost_usd": 0.3,
         "tokens": {"input": 10, "cache_creation": 0, "cache_read": 19000, "output": 5000}, "n_tables": 2,
         "n_rows": 30, "page_kind": "assay_table"},
    ]
    (d / "events.jsonl").write_text("\n".join(json.dumps(e) for e in events) + "\n")
    (d / "calls.jsonl").write_text("\n".join(json.dumps(c) for c in calls) + "\n")
    return d


def test_totals_are_sums_of_what_the_backend_reported(tmp_path: Path) -> None:
    s = summarise_run(run_dir(tmp_path))
    t = s["totals"]
    assert t["calls"] == 2 and t["ok"] == 2 and t["failed"] == 0
    assert t["model_time_s"] == 100.0 and t["cost_usd"] == 0.8
    assert t["output"] == 14000 and t["cache_read"] == 34000
    assert t["per_ok_page"] == {"duration_s": 50.0, "cost_usd": 0.4, "output_tokens": 7000}
    assert s["wall_s"] == 150.0, "wall time spans the plan event to the last call"
    assert s["expected_model"] == "claude-opus-5" and s["off_model_calls"] == 0


def test_a_call_that_resolved_to_another_model_is_counted_not_hidden(tmp_path: Path) -> None:
    s = summarise_run(run_dir(tmp_path, model_second="claude-sonnet-5"))
    assert s["off_model_calls"] == 1
    assert s["models_seen"] == ["claude-opus-5", "claude-sonnet-5"]
    assert "off-model calls 1" in format_report(s)


def test_a_partial_run_with_no_calls_yet_still_summarises(tmp_path: Path) -> None:
    d = tmp_path / "r"
    d.mkdir()
    (d / "events.jsonl").write_text(json.dumps({"at": "2026-09-19T00:00:00+00:00", "event": "plan",
                                                "n_pages": 3, "files": ["X"]}) + "\n")
    s = summarise_run(d)
    assert s["totals"]["calls"] == 0 and "per_ok_page" not in s["totals"] and s["wall_s"] == 0.0
    assert "calls 0" in format_report(s)
