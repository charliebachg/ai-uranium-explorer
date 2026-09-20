"""The fabrication gate, and the panel loop that feeds it.

A published prospectivity assistant restricted to tool-computed scores still fabricated a number in 1 of 150
rated responses. These tests are the reason that cannot happen here: a memo whose numbers do not come from a
tool result is rejected before it reaches the store.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from legacy_reader.prospect import memo as M
from legacy_reader.prospect import tools as T


def val(vid: str, value, fmt: str = "m1", unit: str | None = None) -> dict:
    return {"id": vid, "kind": "stat", "as_printed": None, "value": value, "unit_as_printed": None,
            "fmt": fmt, "unit": unit}


VALUES = {
    "c:cell:0001_0001:d_conductor_m": val("c:cell:0001_0001:d_conductor_m", 820.0, unit="m"),
    "c:score:0001_0001:criteria": val("c:score:0001_0001:criteria", 0.731, fmt="ratio3"),
    "c:near:0001_0001:0": val("c:near:0001_0001:0", 12.4, fmt="m2", unit="km"),
}


def memo(**over) -> dict:
    base = {
        "summary": "A corridor worth a closer look.",
        "claims": [{"text": "The nearest mapped conductor is 820.0 m away.",
                    "value_ids": ["c:cell:0001_0001:d_conductor_m"]}],
        "verdict": "supports a closer look",
        "unknown_criteria": ["lake_water_uranium"],
        "absent_criteria": [],
        "next_observation": "A ground EM line across the corridor.",
    }
    return {**base, **over}


# ---------------------------------------------------------------- the gate


def test_a_memo_whose_numbers_all_cite_tool_values_passes():
    assert M.check_memo(memo(), VALUES) == []


def test_a_fabricated_number_is_caught():
    """The exact failure MineTRACE reported: a number that came from nowhere."""
    bad = memo(claims=[{"text": "The conductor is 820.0 m away and the grade reached 2.4% U3O8.",
                        "value_ids": ["c:cell:0001_0001:d_conductor_m"]}])
    problems = M.check_memo(bad, VALUES)
    assert any("2.4" in p for p in problems)


def test_a_citation_to_a_value_no_tool_returned_is_caught():
    bad = memo(claims=[{"text": "The score is 0.731.", "value_ids": ["c:score:0001_0001:invented"]}])
    problems = M.check_memo(bad, VALUES)
    assert any("no tool returned" in p for p in problems)


def test_a_number_from_another_claims_citation_does_not_count():
    """Each claim must carry its own evidence; borrowing a neighbour's id is not citation."""
    bad = memo(claims=[
        {"text": "The score is 0.731.", "value_ids": ["c:score:0001_0001:criteria"]},
        {"text": "The nearest deposit is 12.4 km away.", "value_ids": ["c:score:0001_0001:criteria"]},
    ])
    problems = M.check_memo(bad, VALUES)
    assert any("12.4" in p for p in problems)


def test_a_value_may_be_written_in_any_reasonable_form():
    for text, vid in (
        ("The conductor is 820 m away.", "c:cell:0001_0001:d_conductor_m"),
        ("The conductor is 820.0 m away.", "c:cell:0001_0001:d_conductor_m"),
        ("The criteria score is 0.73.", "c:score:0001_0001:criteria"),
    ):
        assert M.check_memo(memo(claims=[{"text": text, "value_ids": [vid]}]), VALUES) == [], text


def test_a_ratio_may_be_spoken_as_a_percentage():
    ok = memo(claims=[{"text": "The criteria score sits at 73 on a 0 to 100 scale.",
                       "value_ids": ["c:score:0001_0001:criteria"]}])
    assert M.check_memo(ok, VALUES) == []


def test_small_bare_numbers_in_prose_are_allowed():
    ok = memo(claims=[{"text": "Only 2 of the criteria are known here.", "value_ids": []}])
    assert M.check_memo(ok, VALUES) == []


def test_a_memo_must_name_the_observation_that_would_change_it():
    problems = M.check_memo(memo(next_observation="  "), VALUES)
    assert any("next" in p or "observation" in p for p in problems)


def test_a_verdict_outside_the_scale_is_refused():
    problems = M.check_memo(memo(verdict="drill it"), VALUES)
    assert any("verdict" in p for p in problems)


def test_the_scale_tops_out_at_a_closer_look():
    assert M.VERDICTS[0] == "supports a closer look"
    assert not any("drill" in v for v in M.VERDICTS)


def test_unknown_and_absent_must_both_be_reported():
    problems = M.check_memo({**memo(), "unknown_criteria": None}, VALUES)
    assert any("unknown_criteria" in p for p in problems)


def test_a_memo_with_no_claims_is_refused():
    assert any("no claims" in p for p in M.check_memo(memo(claims=[]), VALUES))


# ---------------------------------------------------------------- the loop


class FakeBackend:
    """A scripted model: returns the given steps in order, so the loop is testable without a model."""

    def __init__(self, steps):
        self.steps = list(steps)
        self.requests = []

    def call(self, req):
        self.requests.append(req)
        step = self.steps.pop(0)

        class R:
            structured = step
            cost_usd = 0.01
            model_resolved = "test-model"

        return R()


def test_the_loop_executes_a_tool_call_then_writes(tmp_path, monkeypatch):
    calls = []

    def fake_call(tool, args):
        calls.append((tool, args))
        return T.ToolResult(tool, args, rows=[{"a": 1}],
                            values={"c:cell:x:y": val("c:cell:x:y", 5.0)})

    monkeypatch.setattr(T, "call", fake_call)
    backend = FakeBackend([
        {"action": "call_tool", "reasoning": "need the features", "tool": "cell_features",
         "args": {"cell_id": "0001_0001"}},
        {"action": "write_memo", "reasoning": "enough", "memo": memo(
            claims=[{"text": "A measured value of 5.0 is present.", "value_ids": ["c:cell:x:y"]}])},
    ])
    out = M.run_role("0001_0001", "proponent", backend, "m", "medium", tmp_path, log=lambda *_: None)
    assert [c[0] for c in calls][1:] == ["cell_features"], "the opening bundle plus the requested tool"
    assert out["published"] is True
    assert out["problems"] == []


def test_the_loop_rejects_a_memo_it_cannot_back(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(tool, args, rows=[], values={}))
    backend = FakeBackend([
        {"action": "write_memo", "reasoning": "", "memo": memo(
            claims=[{"text": "Grades of 4.7% U3O8 were intersected.", "value_ids": []}])},
    ])
    out = M.run_role("0001_0001", "proponent", backend, "m", "medium", tmp_path, log=lambda *_: None)
    assert out["published"] is False
    assert any("4.7" in p for p in out["problems"])


def test_a_bad_tool_name_does_not_end_the_run(tmp_path, monkeypatch):
    def fake_call(tool, args):
        if tool == "nonsense":
            raise T.ToolError("no tool named 'nonsense'")
        return T.ToolResult(tool, args, rows=[], values={})

    monkeypatch.setattr(T, "call", fake_call)
    backend = FakeBackend([
        {"action": "call_tool", "reasoning": "", "tool": "nonsense", "args": {}},
        {"action": "write_memo", "reasoning": "", "memo": memo(claims=[
            {"text": "No numbers here at all.", "value_ids": []}])},
    ])
    out = M.run_role("0001_0001", "proponent", backend, "m", "medium", tmp_path, log=lambda *_: None)
    assert out["published"] is True
    assert (tmp_path / "tool_error_0.json").is_file()


def test_running_out_of_steps_is_recorded_rather_than_guessed(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(tool, args, rows=[], values={}))
    backend = FakeBackend([{"action": "call_tool", "reasoning": "", "tool": "coverage", "args": {}}]
                          * M.MAX_STEPS)
    out = M.run_role("0001_0001", "proponent", backend, "m", "medium", tmp_path, log=lambda *_: None)
    assert out["published"] is False
    assert "ran out of steps" in out["problems"][0]


def test_the_stage_carries_the_handbook_and_the_criteria(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(tool, args, rows=[], values={}))
    backend = FakeBackend([{"action": "write_memo", "reasoning": "", "memo": memo(
        claims=[{"text": "Nothing numeric.", "value_ids": []}])}])
    M.run_role("0001_0001", "proponent", backend, "m", "medium", tmp_path, log=lambda *_: None)
    assert (tmp_path / "handbook.md").is_file() and (tmp_path / "criteria.toml").is_file()
    staged = {name for _p, name in backend.requests[0].stage_files}
    assert {"handbook.md", "criteria.toml"} <= staged


def test_the_bundle_changes_the_cache_key(tmp_path, monkeypatch):
    """A changed bundle must never be answered from an older run's cache."""
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(tool, args, rows=[], values={}))
    backend = FakeBackend([{"action": "write_memo", "reasoning": "", "memo": memo(
        claims=[{"text": "Nothing numeric.", "value_ids": []}])}])
    M.run_role("0001_0001", "proponent", backend, "m", "medium", tmp_path, log=lambda *_: None)
    req = backend.requests[0]
    before = req.cache_key("claude_cli")
    (tmp_path / "handbook.md").write_text("different handbook")
    assert req.cache_key("claude_cli") != before


def test_the_skeptic_is_handed_the_proponents_memo(tmp_path, monkeypatch):
    monkeypatch.setattr(T, "call", lambda tool, args: T.ToolResult(tool, args, rows=[], values={}))
    backend = FakeBackend([{"action": "write_memo", "reasoning": "", "memo": memo(
        claims=[{"text": "Nothing numeric.", "value_ids": []}])}])
    M.run_role("0001_0001", "skeptic", backend, "m", "medium", tmp_path,
               prior={"proponent": memo()}, log=lambda *_: None)
    written = json.loads((tmp_path / "proponent.json").read_text())
    assert written["verdict"] == "supports a closer look"


def test_the_system_prompt_forbids_the_things_that_matter():
    low = M.SYSTEM.lower()
    for phrase in ("may not state any number", "never compute", "unknown is not absent", "never recommend"):
        assert phrase in low, phrase


# ---------------------------------------------------------------- what the gate must not reject

CONTEXT = json.dumps({
    "rows": [{"name": "Drill hole HR-014", "note": "mapped at 1:250,000"},
             {"title": "Lake sediment geochemistry (provincial survey, 1975-1978)"}]
})


def test_a_name_with_digits_in_it_is_not_a_fabricated_number():
    """The first real run rejected three sound memos for writing HR-014, 1:250,000 and 1975-1978."""
    ok = memo(claims=[{"text": "A documented drill hole (HR-014) sits nearby.", "value_ids": []}])
    assert M.check_memo(ok, VALUES, context=CONTEXT) == []


def test_a_map_scale_is_not_a_fabricated_number():
    ok = memo(claims=[{"text": "The bedrock is mapped at 1:250,000.", "value_ids": []}])
    assert M.check_memo(ok, VALUES, context=CONTEXT) == []


def test_a_survey_period_quoted_from_a_tool_result_is_allowed():
    ok = memo(claims=[{"text": "The 1975-1978 survey covers this ground.", "value_ids": []}])
    assert M.check_memo(ok, VALUES, context=CONTEXT) == []


def test_a_number_absent_from_both_the_citations_and_the_tool_results_is_still_caught():
    bad = memo(claims=[{"text": "Grades reached 4.7% U3O8 over 8.5 m.", "value_ids": []}])
    problems = M.check_memo(bad, VALUES, context=CONTEXT)
    assert any("4.7" in p for p in problems)
    assert any("8.5" in p for p in problems)


def test_context_cannot_launder_a_measurement_the_model_invented():
    """A number the tools never mentioned stays rejected however much context is supplied."""
    bad = memo(claims=[{"text": "The unconformity here is 512.7 m deep.", "value_ids": []}])
    assert any("512.7" in p for p in M.check_memo(bad, VALUES, context=CONTEXT))


def test_a_number_followed_by_punctuation_is_read_without_it():
    """The gate once rejected "falling(500 m, 5000 m)" by capturing the comma as part of the number.

    The thresholds are values now rather than prose quoted out of criteria.toml, so the claim cites them; what
    is under test is still the punctuation, which must not become part of the number.
    """
    vals = {"c:lo": val("c:lo", 500.0, fmt="m1"), "c:hi": val("c:hi", 5000.0, fmt="m1")}
    ok = memo(claims=[{"text": "Measured against the falling(500 m, 5000 m) threshold, it is not met.",
                       "value_ids": ["c:lo", "c:hi"]}])
    assert M.check_memo(ok, vals) == []


def test_a_thousands_separator_inside_a_number_still_reads_as_one_number():
    vals = {"c:x": val("c:x", 30534.0, fmt="int")}
    ok = memo(claims=[{"text": "The grid holds 30,534 cells.", "value_ids": ["c:x"]}])
    assert M.check_memo(ok, vals) == []


def test_a_distance_in_metres_may_be_written_in_kilometres():
    """The gate verifies the conversion itself rather than forcing stilted prose."""
    vals = {"c:d": val("c:d", 82885.3, unit="m")}
    ok = memo(claims=[{"text": "The nearest sample is 82.9 km away.", "value_ids": ["c:d"]}])
    assert M.check_memo(ok, vals) == []


def test_a_wrong_conversion_is_still_caught():
    vals = {"c:d": val("c:d", 82885.3, unit="m")}
    bad = memo(claims=[{"text": "The nearest sample is 8.29 km away.", "value_ids": ["c:d"]}])
    assert any("8.29" in p for p in M.check_memo(bad, vals))


def test_a_conversion_is_only_offered_for_units_that_have_one():
    """A concentration in ppm has no scale rendering, so a rescaled number stays a fabrication."""
    vals = {"c:u": val("c:u", 228.0, unit="ppm")}
    bad = memo(claims=[{"text": "Sediment uranium reaches 0.228 there.", "value_ids": ["c:u"]}])
    assert any("0.228" in p for p in M.check_memo(bad, vals))


def test_a_number_glued_to_its_unit_in_quoted_text_is_the_same_number() -> None:
    from legacy_reader.prospect.memo import check_claims, numbers_in

    assert numbers_in("9 holes totalling 2310m in 2013-2014 and 838m; hole PLS12-023 on sheet 74H09") == ["9", "2310", "838"]
    assert numbers_in("2018: drilling resumed. 111.4: sandstone; id c:cell:0201_0072:d_fault_m") == ["2018", "111.4"]
    context = "description = 9 drill holes totalling 2310m in 2013-2014 and 7 holes totalling 838m"
    claims = [{"text": "The passages describe 9 holes totalling 2310 m and 7 holes totalling 838 m.", "value_ids": []}]
    assert check_claims(claims, {}, context) == []
    # a number that is not in the text is still refused
    assert check_claims([{"text": "the holes total 2400 m", "value_ids": []}], {}, context)
