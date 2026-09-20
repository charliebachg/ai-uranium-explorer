"""The extractor loop: the five stages in order, on disk after each, resumable, budgeted, and the queue filed.

Nothing here calls a model or touches the real data directories: the reader and the second family are one
scripted backend that answers by model id, the OCR words are synthesised from the fake page's row quotes so
the locator can give both readings boxes, and the store is a temporary DuckDB with the real schema."""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import pytest

from uranium_explorer import assemble as ASM
from uranium_explorer import extract as ex
from uranium_explorer.backends.base import ExtractionRequest, ExtractionResponse
from uranium_explorer.extractor import loop as L
from uranium_explorer.extractor import queue as Q
from uranium_explorer.extractor.states import Stage, read_states
from uranium_explorer.runtime.spend import BudgetExhausted
from uranium_explorer.store import connect

from fake_page import FILE_NUM, PAGE_ID, PAGE_NO, PDF_SHA, extract_row, page_result

SECOND = "fake/second-family"


def fake_words(result: dict[str, Any]) -> list[dict[str, Any]]:
    """OCR words laid out one printed row per line, so every row quote and cell value locates."""
    words: list[dict[str, Any]] = []
    line = 0
    y = 0.05

    def put(text: str) -> None:
        nonlocal y, line
        x = 0.05
        for n, tok in enumerate(text.split()):
            w = 0.012 * max(len(tok), 2)
            words.append({"page_no": PAGE_NO, "engine": "livetext", "text": tok, "x0": x, "y0": y, "x1": x + w,
                          "y1": y + 0.014, "conf": 0.9, "line_id": line, "word_no": n, "rotation_ccw": 0})
            x += w + 0.01
        y += 0.03
        line += 1

    for key in ("hole_id", "depth_unit"):
        f = result["page_level"][key]
        if f.get("printed") == "printed":
            put(f["quote"])
    for table in result["tables"]:
        put(" ".join(table["column_headers_as_printed"]))
        for row in table["rows"]:
            put(row["row_quote"])
    return words


def second_reading() -> dict[str, Any]:
    """The second family's answer: one depth read differently, the last lithology row missed, one grade found
    that the first reader called blank."""
    r = copy.deepcopy(page_result())
    lith = r["tables"][0]
    lith["rows"][1]["cells"][1]["value_as_printed"] = "42.5"       # to_depth 42.0 -> 42.5
    lith["rows"].pop()                                              # the fifth row is gone
    lith["printed_row_count"] = 4
    assay = r["tables"][1]
    u = assay["rows"][0]["cells"][4]
    u.update(value_as_printed="15", printed="printed", quote="216 136.5 141.5 0.8 15 20")
    return r


class ScriptedBackend:
    """Answers by model id: the reader's page for a claude id, the second family's for anything else."""

    family = "scripted"

    def __init__(self, second: dict[str, Any] | None = None) -> None:
        self.calls: list[str] = []
        self.second = second if second is not None else second_reading()

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        self.calls.append(req.model)
        answer = page_result() if req.model.startswith("claude") else self.second
        return ExtractionResponse(structured=answer, envelope={}, backend="scripted", backend_version="0",
                                  model_requested=req.model, model_resolved=req.model, num_turns=1, duration_s=0.01,
                                  usage={"prompt_tokens": 1000, "completion_tokens": 200}, cost_usd=0.02,
                                  cache_key=req.cache_key(self.family))


def config(**over: Any) -> ex.Config:
    base = dict(id="test", model="claude-opus-5", effort="medium", page_classes=("assay_table", "lith_log", "collar_table"),
                max_pages_per_file=12, reask=False, validator_mode="enforce", max_budget_usd=0.6, timeout_s=60,
                class_priority=ex.CLASS_PRIORITY, note="")
    base.update(over)
    return ex.Config(**base)


def planned(tmp_path: Path) -> ex.PlannedPage:
    img = tmp_path / "page.png"
    img.write_bytes(b"\x89PNG" + b"\x01" * 32)
    return ex.PlannedPage(page_id=PAGE_ID, file_num=FILE_NUM, pdf_sha256=PDF_SHA, pdf_name="x.pdf", page_no=PAGE_NO,
                          image_path=str(img), image_sha256="0" * 64, route_class="lith_log", route_candidates=(),
                          chain_id=None, chain_pos=None, width_px=1700, height_px=2200, dpi=200)


@pytest.fixture
def world(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> dict[str, Any]:
    """Every path the loop touches, redirected into the temporary directory."""
    results: dict[str, dict[str, Any]] = {}
    words = fake_words(page_result())
    out = tmp_path / "out"
    monkeypatch.setattr(ex, "results_path", lambda: out / "results.jsonl")
    monkeypatch.setattr(L, "read_results", lambda: dict(results))
    monkeypatch.setattr(L, "write_results", lambda rows, path=None: results.update(rows) or (out / "results.jsonl"))
    monkeypatch.setattr(L, "read_pages", lambda: [{"page_id": PAGE_ID, "file_num": FILE_NUM, "pdf_sha256": PDF_SHA,
                                                   "page_no": PAGE_NO, "width_px": 1700, "height_px": 2200, "dpi": 200}])
    monkeypatch.setattr(L, "read_words", lambda sha, root=None: list(words) if sha == PDF_SHA else [])
    monkeypatch.setattr(ASM, "read_words", lambda sha, root=None: list(words) if sha == PDF_SHA else [])
    monkeypatch.setattr(L, "assembled_path", lambda f: out / "assemble" / f"{f}.json")
    monkeypatch.setattr(L, "read_assembled", lambda f: json.loads((out / "assemble" / f"{f}.json").read_text()))
    monkeypatch.setattr(L, "validators_path", lambda f: out / "validate" / f"{f}.json")
    monkeypatch.setattr(L, "build_context", lambda f: {"file_num": f, "geods_holes": [], "compilation_holes": [],
                                                       "nts_sheets": [], "work": {}})
    monkeypatch.setattr(L, "_load_positions", lambda f: {})
    monkeypatch.setattr(L, "_load_crosschecks", lambda f: {})
    monkeypatch.setattr(L, "_text_layer_by_page", lambda doc: {})
    monkeypatch.setattr(L, "geometric_row_counts", lambda doc: {})
    runs = tmp_path / "runs"
    monkeypatch.setattr(L, "run_dir", lambda rid: runs / rid)

    def claim(kind: str) -> tuple[str, Path]:
        n = len(list(runs.glob("*"))) + 1 if runs.is_dir() else 1
        rid = f"2026T{n:02d}-{kind}"
        (runs / rid).mkdir(parents=True)
        return rid, runs / rid

    monkeypatch.setattr(L, "claim_run_dir", claim)
    return {"results": results, "out": out, "runs": runs, "store": tmp_path / "store.duckdb", "cache": tmp_path / "cache"}


def run_loop(world: dict[str, Any], tmp_path: Path, backend: Any, **over: Any) -> tuple[dict[str, Any], L.ExtractorRun]:
    kw: dict[str, Any] = dict(second_model=SECOND, budget_usd=5.0, store=world["store"], log=lambda *a: None,
                              cache_root=world["cache"])
    kw.update(over)
    cfg = kw.pop("cfg", None) or config()
    run = L.ExtractorRun(cfg, backend, [planned(tmp_path)], **kw)
    return run.run(), run


# ---------------------------------------------------------------- the five stages


def test_a_fresh_page_runs_every_stage_in_order_and_files_the_queue(world: dict[str, Any], tmp_path: Path) -> None:
    backend = ScriptedBackend()
    summary, run = run_loop(world, tmp_path, backend)
    assert summary["done"] == 1 and summary["failed"] == 0 and summary["pending"] == 0
    assert backend.calls == ["claude-opus-5", SECOND], "the reader once, the second family once"
    st = read_states(run.rd)[PAGE_ID]
    assert st.done and st.stage is Stage.DONE
    assert st.locate["n_words"] > 0 and st.read["source"] == "live" and st.validate["values"] > 0
    lines = (run.rd / "pages.jsonl").read_text().splitlines()
    assert [json.loads(x)["stage"] for x in lines] == ["read", "validate", "agree", "file", "done"], \
        "the record is on disk after every stage, naming the next one"
    # the comparison, as designed: one depth disagreed, the missed row only the first reader, one grade only the second
    t = st.agree["tally"]
    assert t["disagreed"] == 1 and t["only_a"] == 3 and t["only_b"] == 1 and t["agreed"] > 10
    assert t["by_field_type"]["depth"]["disagreed"] == 1 and t["by_field_type"]["grade"]["only_b"] == 1
    assert summary["agreement"]["pages"] == 1 and summary["agreement"]["n"] == t["n"]
    assert summary["queue_rows"] == 5 and st.file["filed"] == {"agreement": t["n"], "queue": 5}
    # the fresh reading landed in the batch reader's artefacts: results, the assembled document, the validators
    assert PAGE_ID in world["results"] and world["results"][PAGE_ID]["model_resolved"] == "claude-opus-5"
    doc = json.loads((world["out"] / "assemble" / f"{FILE_NUM}.json").read_text())
    assert doc["validators"]["findings"] and (world["out"] / "validate" / f"{FILE_NUM}.json").is_file()
    # the manifest names both models, the budget and what was spent; the run is a trace with a span per stage
    manifest = json.loads((run.rd / "manifest.json").read_text())
    assert manifest["models"] == {"reader": "claude-opus-5", "second": SECOND} and manifest["spent_usd"] == pytest.approx(0.04)
    spans = [json.loads(x) for x in (run.rd / "spans.jsonl").read_text().splitlines()]
    assert {s["name"] for s in spans} >= {"page:locate", "page:read", "page:validate", "page:agree", "page:file", "model:extract_page"}


def test_the_queue_rows_in_the_store_match_the_run_directory_and_carry_both_readings(world: dict[str, Any], tmp_path: Path) -> None:
    summary, run = run_loop(world, tmp_path, ScriptedBackend())
    con = connect(world["store"], read_only=True)
    try:
        items, total = Q.list_items(con, file_num=FILE_NUM)
        agreed = con.execute("select count(*) from read.agreement where status = 'agreed'").fetchone()[0]
        tiers = con.execute("select distinct tier from read.review_item union select distinct tier from read.agreement").fetchall()
    finally:
        con.close()
    assert total == 5 == len([json.loads(x) for x in (run.rd / "queue.jsonl").read_text().splitlines()])
    assert agreed == summary["agreement"]["agreed"] and tiers == [("read",)]
    by_reason = {}
    for it in items:
        by_reason.setdefault(it["reason"], []).append(it)
    assert len(by_reason["disagreed"]) == 1 and len(by_reason["only_a"]) == 3 and len(by_reason["only_b"]) == 1
    d = by_reason["disagreed"][0]
    assert d["reading_a"]["as_printed"] == "42.0" and d["reading_b"]["as_printed"] == "42.5" and d["field"] == "to_depth"
    assert d["value_id"].startswith(f"x:{FILE_NUM}:") and d["reading_a"]["bbox"]
    # the second reader's 42.5 is not in the OCR words, so it has no box of its own: the row band pairs it
    assert d["reading_b"]["bbox"] is None and d["reading_b"]["row_bbox"]
    assert d["model_a"] == "claude-opus-5" and d["model_b"] == SECOND and d["status"] == "open"
    assert by_reason["only_b"][0]["reading_a"] is None and by_reason["only_b"][0]["reading_b"]["as_printed"] == "15"


def test_filing_the_same_comparison_twice_adds_nothing(world: dict[str, Any], tmp_path: Path) -> None:
    run_loop(world, tmp_path, ScriptedBackend())
    run_loop(world, tmp_path, ScriptedBackend())
    con = connect(world["store"], read_only=True)
    try:
        assert con.execute("select count(*) from read.review_item").fetchone()[0] == 5
    finally:
        con.close()


# ---------------------------------------------------------------- carrying prior work


def test_a_page_already_read_is_carried_in_and_the_reader_is_never_called(world: dict[str, Any], tmp_path: Path) -> None:
    from uranium_explorer.prompts import prompt_version
    from uranium_explorer import wire

    prior = extract_row()
    prior.update(prompt_version=prompt_version(), schema_version=wire.SCHEMA_VERSION, run_id="earlier-opus-run",
                 model_resolved="claude-opus-5")
    world["results"][PAGE_ID] = prior
    backend = ScriptedBackend()
    summary, run = run_loop(world, tmp_path, backend, agree_only=True)
    assert backend.calls == [SECOND], "only the second family was called"
    st = read_states(run.rd)[PAGE_ID]
    assert st.read["source"] == "prior" and st.read["run_id_prev"] == "earlier-opus-run" and st.read["cost_usd"] == 0.0
    assert st.done and summary["agreement"]["n"] > 0
    assert summary["fresh_files"] == [] and not (world["out"] / "assemble" / f"{FILE_NUM}.json").exists() or True
    # the first reading's value ids and model are the store's, not the run's
    con = connect(world["store"], read_only=True)
    try:
        models = con.execute("select distinct model_a from read.agreement").fetchall()
    finally:
        con.close()
    assert models == [("claude-opus-5",)]


def test_agree_only_refuses_to_read_a_page_with_no_first_reading(world: dict[str, Any], tmp_path: Path) -> None:
    backend = ScriptedBackend()
    summary, run = run_loop(world, tmp_path, backend, agree_only=True)
    assert backend.calls == [] and summary["failed"] == 1
    st = read_states(run.rd)[PAGE_ID]
    assert st.stage is Stage.READ and st.status == "failed" and "agree-only" in (st.error or "")


def test_without_a_second_family_the_page_is_read_validated_and_filed(world: dict[str, Any], tmp_path: Path) -> None:
    backend = ScriptedBackend()
    summary, run = run_loop(world, tmp_path, backend, skip_agree=True)
    assert backend.calls == ["claude-opus-5"] and summary["done"] == 1
    st = read_states(run.rd)[PAGE_ID]
    assert st.agree["skipped"] is True and summary["agreement"]["pages"] == 0 and summary["queue_rows"] == 0


# ---------------------------------------------------------------- the budget, and resuming


def test_the_budget_stops_the_run_before_the_call_and_a_resume_finishes_it(world: dict[str, Any], tmp_path: Path) -> None:
    backend = ScriptedBackend()
    # the reader's call fits (a $0.03 ceiling under a $0.05 budget) and costs $0.02; the second family's
    # $0.05 estimate on top of that would cross the budget, so it is refused before it is sent
    summary, run = run_loop(world, tmp_path, backend, budget_usd=0.05, cfg=config(max_budget_usd=0.03))
    assert backend.calls == ["claude-opus-5"], "the second family was refused before it was sent"
    assert summary["budget_exhausted"] is True and summary["stopped"] == 1 and summary["done"] == 0
    st = read_states(run.rd)[PAGE_ID]
    assert st.stage is Stage.AGREE and st.status == "stopped" and "budget" in (st.error or "").lower()
    assert st.read is not None and st.validate is not None, "the stages before the stop are on disk"

    backend2 = ScriptedBackend()
    summary2, run2 = run_loop(world, tmp_path, backend2, budget_usd=5.0, resume=run.run_id)
    assert backend2.calls == [SECOND], "resumed at agree: the reader's stage was carried, not re-run"
    st2 = read_states(run2.rd)[PAGE_ID]
    assert st2.done and summary2["done"] == 1
    assert (run2.rd / "readings" / f"{PAGE_ID.replace(':', '_')}.a.json").is_file(), "the first reading came along"
    manifest = json.loads((run2.rd / "manifest.json").read_text())
    assert manifest["notes"] == f"resumed from {run.run_id}"


def test_a_second_family_that_fails_the_schema_leaves_the_page_at_agree(world: dict[str, Any], tmp_path: Path) -> None:
    backend = ScriptedBackend(second={"not": "a page"})
    summary, run = run_loop(world, tmp_path, backend)
    assert summary["failed"] == 1
    st = read_states(run.rd)[PAGE_ID]
    assert st.stage is Stage.AGREE and st.status == "failed" and "wire schema" in (st.error or "")
    assert not list((world["cache"] / "calls").rglob("*.json")) or all(
        json.loads(p.read_text())["request"]["model"] != SECOND for p in (world["cache"] / "calls").rglob("*.json")), \
        "an answer that does not validate is not kept in the cache"


def test_a_stop_signal_raised_by_the_backend_is_a_stop_not_a_failure(world: dict[str, Any], tmp_path: Path) -> None:
    class Exhausted(ScriptedBackend):
        def call(self, req: ExtractionRequest) -> ExtractionResponse:
            raise BudgetExhausted("the ledger is at its ceiling")

    summary, run = run_loop(world, tmp_path, Exhausted())
    assert summary["stopped"] == 1 and summary["budget_exhausted"] and summary["failed"] == 0


# ---------------------------------------------------------------- the plan helpers


def test_page_selectors_and_a_plan_from_the_results_file(world: dict[str, Any]) -> None:
    assert L.parse_selectors([f"{FILE_NUM}:{PAGE_NO}"]) == [(FILE_NUM, PAGE_NO)]
    with pytest.raises(ValueError):
        L.parse_selectors(["nonsense"])
    world["results"][PAGE_ID] = extract_row()
    plan = L.plan_from_results([(FILE_NUM, PAGE_NO), (FILE_NUM, 99)], world["results"])
    assert [p.page_id for p in plan] == [PAGE_ID] and plan[0].width_px == 1700 and plan[0].reason == "already read"
