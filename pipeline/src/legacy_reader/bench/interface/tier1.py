"""Tier 1 of the interface benchmark: questions the store answers exactly, and questions it must refuse.

Every answerable kind here is one tool call and one reading of its result, done by code: the gold is the
value id the tool returns, so a run is scored by whether the agent cited that id, never by whether its prose
matched ours. The unanswerable kinds are the other half of the abstention measurement: a feature nobody
measured at that cell, a cell the grid does not have, a quantity the store holds for no cell (an alteration
measurement, a conductance, a discovery date: the recorded gaps of PRD §5; a grade or an intersection: read
per file from the corpus and joined to no cell, PRD §9.6), and a question the product refuses on principle
(who holds the ground, what it would cost, whether to drill). Their gold is abstention with the reason, and
an agent that answers them is wrong in a way the tier can count.

Each maker takes the context, a cell (None for a grid-level kind), the generator's random source, and an
optional `choice`. With `choice` given the maker draws nothing and rebuilds the item from the recorded
choice, which is how the audit regenerates every item and compares it with what was written.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from ...prospect import tools as T
from .items import (
    GRID, NONE, Ctx, Maker, draw, gold_abstain, gold_bool, gold_set, gold_value, make_item,
)
from .reader import NO_SUCH_CELL

#: the geological features a value question may ask for: the ones with a reading rather than a class
FEATURE_POOL = ("d_fault_m", "unconformity_depth_m", "sed_u_max_ppm", "boulder_max_cps", "water_u_max_ppm",
                "fault_density", "conductor_density", "elevation_m", "relief_m", "water_fraction",
                "vegetation_fraction", "bare_fraction", "grain_coherence", "landform_grain_deg")
SCORE_MODELS = ("learned", "criteria", "effort")

#: quantities the store holds for no cell: the question, the words a feature or criterion would have to carry
#: for the store to hold it after all (the builder checks none does), and why it is absent. A grade and an
#: intersection are in the corpus, read per file and never joined to a cell (PRD §9.6 hole_crosscheck), and
#: under B17 the files within 10 km are blind-listed besides: what `retrieve` surfaces is another property's,
#: quoted by its id and distance, never this cell's. The other three are the recorded gaps of PRD §5.
NO_VALUE: dict[str, tuple[str, tuple[str, ...], str]] = {
    "grade": ("What uranium grade did drilling return at cell {bid}?", ("grade",),
              "no cell carries a grade: grades are read per file and joined to no cell (PRD §9.6)"),
    "intersection": ("What was the best mineralised intersection in cell {bid}?", ("intersection",),
                     "no cell carries an intersection: assays are read per file and joined to no cell (PRD §9.6)"),
    "alteration": ("How intense is the clay alteration in the sandstone at cell {bid}?", ("alteration",),
                   "alteration measurements are a recorded gap (PRD §5): no source holds them"),
    "conductance": ("What is the conductance of the nearest EM conductor to cell {bid}?", ("conductance",),
                    "EM conductor attributes are a recorded gap (PRD §5): the layer carries a type, not a conductance"),
    "discovery_year": ("In what year was the nearest occurrence to cell {bid} discovered?", ("discover",),
                       "discovery dates are a recorded gap (PRD §5): the label layers carry none"),
}
#: questions the product refuses on principle (PRD §14: no claim about ground a company holds, no
#: commercial data; GUIDE §6.2: there is no "drill here")
OUT_OF_SCOPE: dict[str, str] = {
    "holder": "Which company holds the ground at cell {bid}?",
    "staking": "Are the claims over cell {bid} open for staking?",
    "price": "What would optioning the ground at cell {bid} cost?",
    "drill_decision": "Should cell {bid} be drilled next season?",
}
#: the tools an outside-grid question may go to: the ones that say `no such cell` rather than return nothing
OUTSIDE_TOOLS = ("label_context", "nearby", "crosscheck")


@dataclass(frozen=True)
class Kind:
    name: str
    cell_based: bool
    answerable: bool
    make: Maker


def _bid(cell: dict[str, Any]) -> str:
    return str(cell["bench_id"])


def _choose(rng: random.Random | None, pool: list[Any]) -> Any:
    if not pool:
        return None
    return rng.choice(sorted(pool, key=str)) if rng is not None else pool[0]


# ---------------------------------------------------------------- answerable, per cell


def _nearest(tier_name: str) -> Maker:
    def make(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
        r = ctx.call("label_context", cell, radius_km=ctx.label_radius_km)
        row = next((x for x in r.rows if x.get("tier") == tier_name), None)
        if row is None:
            return None
        vid = str(row["distance_km_id"])
        return make_item(1, f"nearest_{tier_name}_km", cell,
                         f"How far is the nearest known {tier_name} from cell {_bid(cell)}, in km?",
                         [Ctx.shown("label_context", cell, radius_km=ctx.label_radius_km)],
                         gold_value([vid], r.values, unit="km", note=f"rank {row.get('rank')} of the nearest labels, the cell's own label masked"),
                         {})
    return make


def _criteria_set(state: str, name: str, phrase: str) -> Maker:
    def make(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
        crit, values = ctx.criteria(cell)
        if not crit:
            return None
        keys = sorted(k for k, row in crit.items() if row.get("state") == state)
        ids = [str(crit[k]["membership_id"]) for k in keys if crit[k].get("membership_id")]
        # the set is the tool's `state` column as it stands: a folklore criterion at weight zero is in it when
        # its membership puts it there, because the question asks what the table says, not what counts
        note = ("an unknown criterion carries no membership id: the key set is the answer"
                if state == "unknown" else "the membership ids of the criteria in the set, the tool's state column as it stands")
        return make_item(1, name, cell, f"Which targeting criteria are {phrase} at cell {_bid(cell)}?",
                         [Ctx.shown("criteria_breakdown", cell)], gold_set(keys, ids, values, note=note), {})
    return make


def _criterion_state(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    crit, values = ctx.criteria(cell)
    key = choice["criterion"] if choice else _choose(rng, [k for k, r in crit.items() if r.get("membership_id")])
    row = crit.get(key) if key else None
    if row is None or not row.get("membership_id"):
        return None
    return make_item(1, "criterion_state", cell,
                     f"Is the criterion '{row.get('title')}' ({key}) met at cell {_bid(cell)}?",
                     [Ctx.shown("criteria_breakdown", cell)],
                     gold_bool(row.get("state") == "met", [str(row["membership_id"])], values,
                               note="met at membership 0.5 and above, as the tool states it"),
                     {"criterion": key})


def _nearby_count(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    layer = choice["layer"] if choice else _choose(rng, list(ctx.nearby_layers))
    if layer not in ctx.nearby_layers:
        return None
    r = ctx.call("nearby", cell, layer=layer, radius_m=ctx.nearby_radius_m, k=3)
    if r.note == NO_SUCH_CELL or not r.rows:
        return None
    vid = str(r.rows[0]["n_within_id"])
    return make_item(1, "nearby_count", cell,
                     f"How many {layer} features are mapped within {ctx.nearby_radius_m:g} m of cell {_bid(cell)}?",
                     [Ctx.shown("nearby", cell, layer=layer, radius_m=ctx.nearby_radius_m, k=3)],
                     gold_value([vid], r.values, note="zero is an answer: nothing mapped inside the radius"),
                     {"layer": layer})


def _nearby_nearest(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    layer = choice["layer"] if choice else _choose(rng, list(ctx.nearby_layers))
    if layer not in ctx.nearby_layers:
        return None
    r = ctx.call("nearby", cell, layer=layer, radius_m=ctx.nearby_radius_m, k=3)
    if r.note == NO_SUCH_CELL or not r.rows or not r.rows[0].get("nearest_m_id"):
        return None
    vid = str(r.rows[0]["nearest_m_id"])
    return make_item(1, "nearby_nearest", cell,
                     f"How far from cell {_bid(cell)} is the nearest {layer} feature within {ctx.nearby_radius_m:g} m?",
                     [Ctx.shown("nearby", cell, layer=layer, radius_m=ctx.nearby_radius_m, k=3)],
                     gold_value([vid], r.values, unit="m"), {"layer": layer})


def _feature(name: str, key: str, question: str, note: str = "") -> Maker:
    def make(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
        feats, values = ctx.features(cell)
        row = feats.get(key)
        if row is None or row.get("value") is None:
            return None
        return make_item(1, name, cell, question.format(bid=_bid(cell)), [Ctx.shown("cell_features", cell)],
                         gold_value([str(row["value_id"])], values, unit=row.get("unit"), note=note), {})
    return make


def _score(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    model = choice["model"] if choice else _choose(rng, list(SCORE_MODELS))
    if model not in SCORE_MODELS:
        return None
    r = ctx.call("cell_scores", cell)
    row = next((x for x in r.rows if x.get("model") == model and x.get("score_id")), None)
    if row is None:
        return None
    return make_item(1, "score", cell, f"What is the {model} score of cell {_bid(cell)}?",
                     [Ctx.shown("cell_scores", cell)],
                     gold_value([str(row["score_id"])], r.values,
                                note=f"out-of-fold: a model fitted on the other spatial folds, fold {row.get('fold')}"),
                     {"model": model})


def _feature_value(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    feats, values = ctx.features(cell)
    key = choice["feature"] if choice else _choose(
        rng, [k for k in FEATURE_POOL if feats.get(k, {}).get("value") is not None])
    row = feats.get(key) if key else None
    if row is None or key not in FEATURE_POOL or row.get("value") is None:
        return None
    return make_item(1, "feature_value", cell,
                     f"What is the value of {row.get('title')} ({key}) at cell {_bid(cell)}?",
                     [Ctx.shown("cell_features", cell)],
                     gold_value([str(row["value_id"])], values, unit=row.get("unit")), {"feature": key})


def _observations(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    feats, values = ctx.features(cell)
    key = choice["feature"] if choice else _choose(
        rng, [k for k, r in feats.items() if r.get("observations_id") and not r.get("is_effort")])
    row = feats.get(key) if key else None
    if row is None or not row.get("observations_id"):
        return None
    return make_item(1, "observations", cell,
                     f"How many observations sit behind {row.get('title')} ({key}) at cell {_bid(cell)}?",
                     [Ctx.shown("cell_features", cell)],
                     gold_value([str(row["observations_id"])], values, note="the count carries its own id"),
                     {"feature": key})


def _crossings(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    r = ctx.call("crosscheck", cell)
    row = next((x for x in r.rows if x.get("pair") == "conductor_fault"), None)
    if r.note == NO_SUCH_CELL or row is None or not row.get("crossings_n_id"):
        return None
    return make_item(1, "crosscheck_crossings", cell,
                     f"How many conductor-fault crossings are there within {row.get('radius_m'):g} m of cell {_bid(cell)}?",
                     [Ctx.shown("crosscheck", cell)],
                     gold_value([str(row["crossings_n_id"])], r.values, note="zero crossings is a real answer"), {})


def _thin_sampling(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    r = ctx.call("crosscheck", cell)
    row = next((x for x in r.rows if x.get("pair") == "sediment_sampling"), None)
    if r.note == NO_SUCH_CELL or row is None or row.get("thin_sampling") is None or not row.get("sed_samples_n_id"):
        return None
    return make_item(1, "thin_sampling", cell,
                     f"Is the lake-sediment sampling within reach of cell {_bid(cell)} thin?",
                     [Ctx.shown("crosscheck", cell)],
                     gold_bool(bool(row["thin_sampling"]), [str(row["sed_samples_n_id"]), str(row["min_samples_id"])],
                               r.values, note="fewer samples than the fixed minimum within reach"), {})


# ---------------------------------------------------------------- answerable, grid-level


def _coverage(name: str, thin: bool) -> Maker:
    def make(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
        table = ctx.reader.call("coverage", {})
        keys = sorted(str(r["feature"]) for r in table.rows if r.get("coverage_id"))
        key = choice["feature"] if choice else _choose(rng, keys)
        if key not in keys:
            return None
        r = ctx.reader.call("coverage", {"feature_key": key})
        row = next((x for x in r.rows if x.get("feature") == key), None)
        if row is None:
            return None
        title = key.replace("_", " ")
        if thin:
            question = f"Is {title} ({key}) a thin feature on this grid?"
            gold = gold_bool(bool(row.get("thin")), [str(row["coverage_id"])], r.values,
                             note="thin as the readiness table flags it, beside the share it rests on")
        else:
            question = f"What share of the grid has an observation behind {title} ({key})?"
            gold = gold_value([str(row["coverage_id"])], r.values, note="a share of cells, 0 to 1")
        return make_item(1, name, None, question, [{"tool": "coverage", "args": {"feature_key": key}}], gold,
                         {"feature": key}, stratum=GRID)
    return make


# ---------------------------------------------------------------- unanswerable


def _not_measured(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    feats, fvalues = ctx.features(cell)
    crit, cvalues = ctx.criteria(cell)
    if choice is None:
        pool: list[dict[str, str]] = [{"feature": k} for k, r in feats.items()
                                      if r.get("value") is None and not r.get("text") and not r.get("is_effort")]
        pool += [{"criterion": k} for k, r in crit.items() if r.get("state") == "unknown"]
        choice = _choose(rng, pool) if pool else None
        if choice is None:
            return None
    if "feature" in choice:
        row = feats.get(choice["feature"])
        if row is None or row.get("value") is not None or row.get("text"):
            return None
        near = [str(row["nearest_observation_id"])] if row.get("nearest_observation_id") else []
        return make_item(1, "not_measured", cell,
                         f"What is the value of {row.get('title')} ({choice['feature']}) at cell {_bid(cell)}?",
                         [Ctx.shown("cell_features", cell)],
                         gold_abstain("not_measured", near, fvalues,
                                      note="no observation behind this feature here; not a low value"
                                           + (", and the nearest observation's distance is the evidence" if near else "")),
                         dict(choice))
    row = crit.get(choice.get("criterion", ""))
    if row is None or row.get("state") != "unknown":
        return None
    return make_item(1, "not_measured", cell,
                     f"What is the membership of the criterion '{row.get('title')}' ({choice['criterion']}) at cell {_bid(cell)}?",
                     [Ctx.shown("criteria_breakdown", cell)],
                     gold_abstain("not_measured", [], {}, note="no value for this criterion here: unknown, not absent"),
                     dict(choice))


_OUTSIDE_QUESTIONS = {
    "label_context": "How far is the nearest known deposit from cell {bid}, in km?",
    "nearby": "How many em_conductors features are mapped within {r:g} m of cell {bid}?",
    "crosscheck": "How many conductor-fault crossings are there within {r:g} m of cell {bid}?",
}


def _outside_grid(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    """A bench-style id no benchmark cell carries, put to a tool that says so."""
    if choice is None:
        bogus = f"b-{rng.randint(9000, 9999):04d}"
        tool = rng.choice(OUTSIDE_TOOLS)
        choice = {"bench_id": bogus, "tool": tool}
    bogus, tool = str(choice["bench_id"]), str(choice["tool"])
    if bogus in ctx.bench_ids or tool not in OUTSIDE_TOOLS:
        return None
    args: dict[str, Any] = {"cell_id": bogus}
    if tool == "nearby":
        args |= {"layer": "em_conductors", "radius_m": ctx.nearby_radius_m, "k": 3}
    r = ctx.reader.call(tool, args)
    if r.note != NO_SUCH_CELL:
        return None
    radius = ctx.nearby_radius_m if tool == "nearby" else T.CROSSCHECK_RADIUS_M
    fake = {"bench_id": bogus, "cell_id": bogus, "stratum": NONE, "fold": None}
    item = make_item(1, "outside_grid", fake, _OUTSIDE_QUESTIONS[tool].format(bid=bogus, r=radius),
                     [{"tool": tool, "args": args}],
                     gold_abstain("outside_grid", note="the tool answers `no such cell`"), dict(choice), stratum=NONE)
    item["cell_id"] = None
    item["bench_value_ids"] = None
    return item


def _no_value(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    quantity = choice["quantity"] if choice else _choose(rng, list(NO_VALUE))
    if quantity not in NO_VALUE:
        return None
    question, probes, why = NO_VALUE[quantity]
    # the cell record must hold nothing of the kind: no feature and no criterion carries the word
    feats, _ = ctx.features(cell)
    crit, _ = ctx.criteria(cell)
    names = [f"{k} {r.get('title') or ''}" for k, r in feats.items()] + [f"{k} {r.get('title') or ''}" for k, r in crit.items()]
    if any(p in n.lower() for p in probes for n in names):
        return None
    return make_item(1, "no_value", cell, question.format(bid=_bid(cell)),
                     [Ctx.shown("cell_features", cell)], gold_abstain("no_value", note=why), {"quantity": quantity})


def _out_of_scope(ctx: Ctx, cell: Any, rng: Any, choice: Any) -> dict[str, Any] | None:
    topic = choice["topic"] if choice else _choose(rng, list(OUT_OF_SCOPE))
    if topic not in OUT_OF_SCOPE:
        return None
    return make_item(1, "out_of_scope", cell, OUT_OF_SCOPE[topic].format(bid=_bid(cell)), [],
                     gold_abstain("out_of_scope", note="not a question about the evidence record (PRD §14)"),
                     {"topic": topic})


# ---------------------------------------------------------------- the registry


KINDS: dict[str, Kind] = {k.name: k for k in (
    Kind("nearest_deposit_km", True, True, _nearest("deposit")),
    Kind("nearest_occurrence_km", True, True, _nearest("occurrence")),
    Kind("criteria_unknown", True, True, _criteria_set("unknown", "criteria_unknown", "unknown")),
    Kind("criteria_not_met", True, True, _criteria_set("not met", "criteria_not_met", "not met")),
    Kind("criterion_state", True, True, _criterion_state),
    Kind("coverage_share", False, True, _coverage("coverage_share", thin=False)),
    Kind("coverage_thin", False, True, _coverage("coverage_thin", thin=True)),
    Kind("nearby_count", True, True, _nearby_count),
    Kind("nearby_nearest", True, True, _nearby_nearest),
    Kind("nearest_conductor_m", True, True, _feature(
        "nearest_conductor_m", "d_conductor_m", "How far is the nearest mapped EM conductor from cell {bid}?")),
    Kind("score", True, True, _score),
    Kind("holes_count", True, True, _feature(
        "holes_count", "holes_n", "How many provincial drillhole collars are there within 2 km of cell {bid}?",
        note="an effort feature: where people looked, not what is in the rock")),
    Kind("feature_value", True, True, _feature_value),
    Kind("observations", True, True, _observations),
    Kind("crosscheck_crossings", True, True, _crossings),
    Kind("thin_sampling", True, True, _thin_sampling),
    Kind("not_measured", True, False, _not_measured),
    Kind("outside_grid", False, False, _outside_grid),
    Kind("no_value", True, False, _no_value),
    Kind("out_of_scope", True, False, _out_of_scope),
)}


def generate(ctx: Ctx, targets: dict[str, int], rng: random.Random) -> tuple[list[dict[str, Any]], dict[str, int]]:
    """Every kind the spec asks for, in the spec's order; items unnumbered, shortfalls per kind."""
    items: list[dict[str, Any]] = []
    short: dict[str, int] = {}
    for name, n in targets.items():
        kind = KINDS[name]
        made, missing = draw(name, n, kind.make, ctx, rng, cell_based=kind.cell_based)
        items += made
        if missing > 0:
            short[name] = missing
    return items, short


def regenerate(ctx: Ctx, item: dict[str, Any]) -> dict[str, Any] | None:
    """The item rebuilt from its recorded choice, for the audit. None when the store no longer yields it."""
    kind = KINDS[item["kind"]]
    cell = None
    if kind.cell_based:
        cell = {"bench_id": item["bench_id"], "cell_id": item["cell_id"], "stratum": item["stratum"], "fold": item.get("fold")}
    return kind.make(ctx, cell, None, dict(item.get("choice") or {}))
