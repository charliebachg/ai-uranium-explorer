"""The gate is the load-bearing claim of this project, so it gets an adversarial suite of its own.

These tests are the regression for what `ue prospect gate-eval` measured: the context allowance used to be a
substring test over the whole tool payload, which let a real number be cited to the wrong value and pass. They
also pin the four cases the allowance was added for in the first place, so tightening it cannot quietly
reintroduce the false positives it was meant to remove.
"""

from __future__ import annotations

import random

import pytest

from uranium_explorer.prospect.gate_eval import CORRUPTIONS, Case, Pool, _forms, _say, cases_for
from uranium_explorer.prospect.memo import check_claims, quotable

VALUES = {
    "d_conductor": {"id": "d_conductor", "kind": "stat", "value": 1500.0, "fmt": "m1", "unit": "m",
                    "note": "distance to nearest conductor"},
    "n_holes": {"id": "n_holes", "kind": "stat", "value": 229, "fmt": "int", "note": "drillholes in the cell"},
    "score": {"id": "score", "kind": "stat", "value": 0.347, "fmt": "m2", "note": "criteria score"},
}


def claim(text: str, *ids: str) -> dict:
    return {"text": text, "value_ids": list(ids)}


# ---------------------------------------------------------------- what the allowance is for


def test_quotable_keeps_text_and_drops_numbers() -> None:
    payload = {"rows": [{"hole": "HR-014", "depth_m": 431.5}], "values": {"v1": {"value": 0.347}}}
    text = quotable(payload)
    assert "HR-014" in text
    assert "431.5" not in text
    assert "0.347" not in text


@pytest.mark.parametrize("text", [
    "The nearest hole is HR-014.",
    "The bedrock sheet is published at 1:250,000.",
    "The survey ran 1975-1978.",
])
def test_names_scales_and_periods_are_not_read_as_numbers(text: str) -> None:
    """The four false positives the gate produced in real runs. None of them may come back."""
    assert check_claims([claim(text)], VALUES, context=quotable({"rows": [text]})) == []


def test_a_conversion_the_gate_can_verify_itself_passes() -> None:
    assert check_claims([claim("The nearest conductor is 1.5 km away.", "d_conductor")], VALUES) == []


# ---------------------------------------------------------------- what it must refuse


def test_a_real_number_cited_to_the_wrong_value_is_refused() -> None:
    """The hardest case: nothing is invented, the number is simply attached to the wrong thing.

    Under the old whole-payload allowance the gate caught none of these, because the number was genuinely
    somewhere in the session. The citation has to mean something or it is decoration.
    """
    context = quotable({"values": VALUES})
    problems = check_claims([claim("The criteria score is 229.", "score")], VALUES, context=context)
    assert problems and "229" in problems[0]


def test_an_uncited_number_is_refused_even_when_a_tool_returned_it() -> None:
    context = quotable({"values": VALUES})
    assert check_claims([claim("There are 229 drillholes.")], VALUES, context=context)


def test_citing_an_unknown_value_is_refused() -> None:
    problems = check_claims([claim("The score is 0.347.", "no_such_value")], VALUES)
    assert any("no tool returned" in p for p in problems)


@pytest.mark.parametrize("token,why", [
    ("0.348", "one digit moved"),
    ("3.47", "the decimal moved"),
    ("0.3471", "precision the value does not have"),
    ("0.52", "invented outright"),
])
def test_corrupted_numbers_are_refused(token: str, why: str) -> None:
    assert check_claims([claim(f"The criteria score is {token}.", "score")], VALUES), why


# ---------------------------------------------------------------- the suite generator


def test_every_honest_rendering_passes() -> None:
    for vid, val in VALUES.items():
        for token in _forms(val):
            assert check_claims([claim(_say(vid, val, token), vid)], VALUES) == [], f"{vid} written as {token}"


def test_every_corruption_changes_the_number() -> None:
    rng = random.Random(0)
    for name, corrupt in CORRUPTIONS.items():
        token = corrupt(1500.0, rng)
        assert token is None or token != "1500.0", name


def test_cases_cover_both_directions() -> None:
    pool = Pool(cell_id="0000_0000", values=dict(VALUES), context=quotable({"values": VALUES}))
    cases = cases_for(pool, random.Random(1), per_cell=3)
    assert any(c.honest for c in cases)
    assert any(not c.honest for c in cases)
    assert {"cited_to_the_wrong_value", "uncited"} <= {c.kind for c in cases}


def test_a_generated_case_knows_what_it_is() -> None:
    case = Case("digit_slip", False, "0000_0000", "The criteria score is 0.348.", ["score"], "0.348")
    assert case.claim() == {"text": "The criteria score is 0.348.", "value_ids": ["score"]}


# ---------------------------------------------------------------- the invariant behind both


def _numbers_in(node: object, out: list[float]) -> None:
    if isinstance(node, bool):
        return
    if isinstance(node, (int, float)):
        out.append(float(node))
    elif isinstance(node, dict):
        for v in node.values():
            _numbers_in(v, out)
    elif isinstance(node, list):
        for v in node:
            _numbers_in(v, out)


@pytest.mark.parametrize("tool", ["cell_features", "cell_scores", "criteria_breakdown", "label_context"])
def test_every_number_a_tool_shows_the_model_is_citable(tool: str) -> None:
    """A number the model can read but cannot cite is a trap: it will state it, and the gate will refuse it.

    This happened for real. `cell_features` printed an observation count with no value id, a skeptic memo said
    "2.3 ppm from 23 observations", and the gate rejected the whole memo for a number the agent had no way to
    back. The rule is not "most numbers are values" — it is that showing a number and refusing to let it be
    cited is a bug in the tool, not in the model.
    """
    from uranium_explorer.prospect import tools as T
    from uranium_explorer.prospect.memo import BARE_OK, _formatted
    from uranium_explorer.store import connect

    from conftest import skip_without_store

    skip_without_store()
    con = connect(read_only=True)
    try:
        row = con.execute("select cell_id from derived.cell_score where score is not null limit 1").fetchone()
    finally:
        con.close()
    if not row:
        pytest.skip("no scored cells in the store")

    result = T.call(tool, {"cell_id": row[0]})
    citable: set[str] = set(BARE_OK)
    for val in result.values.values():
        citable |= _formatted(val)
    shown: list[float] = []
    _numbers_in(result.rows, shown)
    uncitable = [n for n in shown
                 if not ({str(n), f"{n:g}", f"{n:.0f}"} & citable)]
    assert not uncitable, f"{tool} shows {sorted(set(uncitable))} with no value id to cite"
