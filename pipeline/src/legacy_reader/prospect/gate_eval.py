"""Does the fabrication gate actually catch a fabricated number?

Fifteen memos and answers have gone through the gate. Four were rejected, and every one of those four was a
false positive: a hole name, a map scale, a trailing comma, a unit conversion. So the honest reading of the
record so far is not "the gate works" — it is "the model has not yet fabricated anything, and the gate has
never once fired correctly". A check that has only ever been wrong when it fired is an untested check, and
claiming otherwise is exactly the kind of unearned confidence this project exists to avoid.

This module tests it directly, with no model in the loop. It takes real evidence packs from the store, writes
claims that are true of them, then corrupts those claims the way a language model plausibly would — a digit
slips, a decimal moves, a conversion is done by hand, a number is right but cited to the wrong value, a number
is simply invented — and asks the gate to sort them. Because nothing is generated, the suite is deterministic,
free, and can run in CI on every change to the gate.

Two conditions are reported, and the gap between them is the price of letting the agent quote text at all:

* **cited** — the claim is judged only against the values it cites. This is the rule as stated, and the ceiling.
* **production** — plus any number appearing in the *text* a tool returned: a hole name, a map scale, a survey
  period, a sentence quoted off a scanned page. This is what ships, so it is the number to quote.

Running it changed the system three times, which is the argument for having written it:

1. The allowance used to be a substring test over the whole tool payload. That meant every number the tools
   returned was quotable in every claim, so the value-id binding did nothing: the suite caught **none** of the
   40 cases where a real number was attached to the wrong value. It now reads only the string leaves, with the
   same number scanner it uses on the claim, so a coincidence inside a cell id is not a quotation.
2. `cell_features` printed an observation count with no value id. A real skeptic memo then wrote "2.3 ppm from
   23 observations" and was rejected for a number it had no way to cite. Showing the model a number it cannot
   cite is a bug in the tool; every number a tool prints now carries an id, and a test enforces it.
3. The handbook and the criteria file used to be part of the allowance, which made every threshold and weight
   in the criteria table an uncited number the agent could put on screen. They are values now, so the text
   allowance was removed and the memos still publish.

What this does not measure: whether a model ever writes such a claim. That needs the model, and it is the
denominator problem the panel eval addresses separately. This measures the gate, which is the part that is
supposed to hold when the model is wrong.

"""

from __future__ import annotations

import datetime as dt
import random
from dataclasses import dataclass, field
from typing import Any, Callable

from ..ids import sha256_json, short
from . import tools as T
from .memo import _formatted, check_claims, quotable

EVAL_VERSION = "prospect/gate_eval/v1"

#: the tools a conversation stages before the first question; the evidence any claim is written against
OPENING = ("cell_scores", "cell_features", "criteria_breakdown", "label_context")


@dataclass
class Case:
    """One claim put to the gate, and what the gate was supposed to say about it."""

    kind: str
    honest: bool
    cell_id: str
    text: str
    value_ids: list[str]
    token: str
    note: str = ""

    def claim(self) -> dict[str, Any]:
        return {"text": self.text, "value_ids": list(self.value_ids)}


@dataclass
class Pool:
    """The evidence record for one cell: what the tools returned, and the prose the agent would have seen."""

    cell_id: str
    values: dict[str, dict[str, Any]] = field(default_factory=dict)
    context: str = ""

    @property
    def numeric(self) -> list[tuple[str, dict[str, Any]]]:
        return [(vid, v) for vid, v in sorted(self.values.items())
                if isinstance(v.get("value"), (int, float)) and not isinstance(v.get("value"), bool)]


def build_pool(cell_id: str) -> Pool:
    """Exactly the evidence a conversation opens with, assembled without calling a model."""
    pool = Pool(cell_id=cell_id)
    for tool in OPENING:
        result = T.call(tool, {"cell_id": cell_id})
        pool.values |= result.values
        pool.context += "\n" + quotable(result.as_json())
    return pool


def _label(vid: str, val: dict[str, Any]) -> str:
    """How a claim would name the thing it is stating, so the sentence reads like one an agent would write."""
    note = str(val.get("note") or "").strip().rstrip(".")
    return note if note else vid.replace("_", " ")


def _say(vid: str, val: dict[str, Any], token: str) -> str:
    """A claim sentence carrying exactly one number, spaced so the gate's number scanner sees it."""
    unit = str(val.get("unit") or "")
    return f"{_label(vid, val)} is {token}{' ' + unit if unit else ''}."


def _forms(val: dict[str, Any]) -> list[str]:
    """Ways of writing this value that a careful writer might choose, each one true."""
    v = float(val["value"])
    out = [str(val["value"])]
    if v.is_integer():
        out.append(f"{int(v):,}")
    else:
        out += [f"{v:.1f}", f"{v:.2f}"]
    if str(val.get("unit") or "") == "m" and abs(v) >= 1000:
        out.append(f"{v / 1000:.1f}")  # the gate verifies this conversion itself
    if 0 < v < 1:
        out.append(f"{v * 100:.0f}")  # a ratio spoken as a percentage
    seen: list[str] = []
    for f in out:
        if f not in seen:
            seen.append(f)
    return seen


# ---------------------------------------------------------------- corruptions
# Each takes a true value and returns a number that is not that value. They are the failures reported for
# language models writing about data: a digit slips, a decimal moves, arithmetic is done in the head, a real
# number is attached to the wrong thing, or a plausible figure is invented whole.


def _digit_slip(v: float, rng: random.Random) -> str | None:
    digits = [c for c in f"{v}" if c.isdigit()]
    if not digits:
        return None
    text = f"{v}"
    positions = [i for i, c in enumerate(text) if c.isdigit()]
    i = rng.choice(positions)
    replacement = rng.choice([d for d in "0123456789" if d != text[i]])
    return text[:i] + replacement + text[i + 1:]


def _decimal_shift(v: float, rng: random.Random) -> str | None:
    shifted = v * rng.choice([10.0, 0.1])
    return f"{shifted:g}"


def _transpose(v: float, _rng: random.Random) -> str | None:
    text = f"{v}"
    positions = [i for i in range(len(text) - 1) if text[i].isdigit() and text[i + 1].isdigit()]
    for i in positions:
        if text[i] != text[i + 1]:
            return text[:i] + text[i + 1] + text[i] + text[i + 2:]
    return None


def _false_precision(v: float, rng: random.Random) -> str | None:
    return f"{v:.3f}{rng.randint(1, 9)}"


def _hand_conversion(v: float, _rng: random.Random) -> str | None:
    """Metres to kilometres divided by 100 instead of 1000: the classic silent unit error."""
    return f"{v / 100:g}"


def _invented(v: float, rng: random.Random) -> str | None:
    """A figure of the right shape and order of magnitude, related to nothing."""
    scale = max(abs(v), 1.0)
    made = rng.uniform(0.3 * scale, 2.5 * scale)
    return f"{made:.1f}" if scale < 100 else f"{made:.0f}"


CORRUPTIONS: dict[str, Callable[[float, random.Random], str | None]] = {
    "digit_slip": _digit_slip,
    "decimal_shift": _decimal_shift,
    "transposed": _transpose,
    "false_precision": _false_precision,
    "hand_conversion": _hand_conversion,
    "invented": _invented,
}


def cases_for(pool: Pool, rng: random.Random, per_cell: int = 8) -> list[Case]:
    """Honest claims and corrupted ones over the same evidence, so both error directions are measured."""
    numeric = pool.numeric
    if len(numeric) < 2:
        return []
    picks = rng.sample(numeric, k=min(per_cell, len(numeric)))
    out: list[Case] = []

    for vid, val in picks:
        for token in _forms(val):
            out.append(Case("stated_plainly", True, pool.cell_id, _say(vid, val, token), [vid], token))

    for vid, val in picks:
        v = float(val["value"])
        true_forms = _formatted(val)
        for kind, corrupt in CORRUPTIONS.items():
            if kind == "hand_conversion" and str(val.get("unit") or "") != "m":
                continue
            token = corrupt(v, rng)
            if token is None or token in true_forms or token.replace(",", "") in true_forms:
                continue  # the corruption landed back on the truth; it is not a fabrication to catch
            out.append(Case(kind, False, pool.cell_id, _say(vid, val, token), [vid], token))

    # A number that is real, and cited to something it is not about. Nothing is invented here at all, which is
    # why it is the hardest case: only the value id binding catches it.
    for vid, val in picks:
        others = [(o, ov) for o, ov in numeric
                  if o != vid and str(ov["value"]) not in _formatted(val)]
        if not others:
            continue
        borrowed, bval = rng.choice(others)
        out.append(Case("cited_to_the_wrong_value", False, pool.cell_id,
                        _say(vid, val, str(bval["value"])), [vid], str(bval["value"]),
                        note=f"the number belongs to {borrowed}"))

    # A number stated with no citation at all.
    for vid, val in picks[:2]:
        out.append(Case("uncited", False, pool.cell_id, _say(vid, val, str(val["value"])), [], str(val["value"])))

    return out


def run(cell_ids: list[str], seed: int = 20260918, per_cell: int = 8,
        log: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Put every case to the gate under both conditions and report what it sorted correctly."""
    rng = random.Random(seed)
    cases: list[Case] = []
    pools: dict[str, Pool] = {}
    for cell_id in cell_ids:
        pool = build_pool(cell_id)
        pools[cell_id] = pool
        made = cases_for(pool, rng, per_cell=per_cell)
        cases += made
        log(f"    {cell_id}: {len(pool.values)} values, {len(made)} cases")

    conditions: dict[str, Any] = {}
    by_kind: dict[str, dict[str, int]] = {}
    escaped: list[dict[str, Any]] = []
    wrongly_rejected: list[dict[str, Any]] = []

    for condition in ("cited", "production"):
        caught = missed = passed = rejected = 0
        for case in cases:
            pool = pools[case.cell_id]
            context = pool.context if condition == "production" else ""
            problems = check_claims([case.claim()], pool.values, context=context)
            if case.honest:
                if problems:
                    rejected += 1
                    if condition == "production" and len(wrongly_rejected) < 10:
                        wrongly_rejected.append({"text": case.text, "why": problems[0]})
                else:
                    passed += 1
            else:
                if problems:
                    caught += 1
                else:
                    missed += 1
                    if condition == "production" and len(escaped) < 12:
                        escaped.append({"kind": case.kind, "text": case.text, "note": case.note})
                if condition == "production":
                    row = by_kind.setdefault(case.kind, {"caught": 0, "missed": 0})
                    row["caught" if problems else "missed"] += 1
        honest = sum(1 for c in cases if c.honest)
        fabricated = len(cases) - honest
        conditions[condition] = {
            "fabrications_put": fabricated,
            "fabrications_caught": caught,
            "fabrications_missed": missed,
            "caught_rate": round(caught / fabricated, 4) if fabricated else None,
            "honest_put": honest,
            "honest_passed": passed,
            "honest_rejected": rejected,
            "false_rejection_rate": round(rejected / honest, 4) if honest else None,
        }
        log(f"  {condition}: caught {caught}/{fabricated} fabrications, "
            f"wrongly rejected {rejected}/{honest} honest claims")

    return {
        "eval_version": EVAL_VERSION,
        "run_id": short(sha256_json([EVAL_VERSION, seed, sorted(cell_ids), per_cell])),
        "seed": seed,
        "cells": sorted(cell_ids),
        "cases": len(cases),
        "conditions": conditions,
        "by_kind": by_kind,
        "escaped": escaped,
        "wrongly_rejected": wrongly_rejected,
        "ran_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }
