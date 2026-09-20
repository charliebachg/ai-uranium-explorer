"""What an interface item is, and how cells are drawn for it.

An item is a question, the tool calls a correct answer makes, and a gold that is checked by value ids. Four
gold shapes cover both tiers:

* `value` — one or more value ids and their values (a distance, a count, a score);
* `set` — a set of keys from a closed vocabulary (criterion keys) with the ids of the members that carry one;
  an unknown criterion has no membership id, so the key set is the answer and the ids are the evidence;
* `bool` — yes or no, with the ids that decide it;
* `abstain` — no answer, with a reason from `REASONS` and any ids that show why (the nearest observation
  behind an unmeasured feature, for instance).

Questions name the cell by its bench id, as a benchmark session shows it; the real cell id sits beside it
in the item for the builder and the audit, as `cells.jsonl` does for the analyst track. Value ids are the
store's (`c:…`); `bench_value_ids` are the same ids as a benchmark session over the cell rewrites them.
Cells are drawn per kind evenly over the four strata, each stratum's cells visited in one seeded rotation
that runs on from kind to kind, so the items of a kind are spread across the stratum's cells rather than
piled on its first few.
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field
from typing import Any, Callable

from ...ids import canonical_json
from ...prospect import tools as T
from .. import pack as P
from ..spec import STRATA
from .reader import Reader

REASONS = ("not_measured", "outside_grid", "no_value", "out_of_scope")
ANSWERS = ("value", "set", "bool", "abstain")
#: wording no item may carry, in a question, a premise or a note
BANNED = ("high potential", "drill target", "prospective ground")
#: the stratum of an item that names no cell, and of one that names a cell the grid does not have
GRID = "grid"
NONE = "none"

Maker = Callable[["Ctx", dict[str, Any] | None, random.Random, dict[str, Any] | None], dict[str, Any] | None]


class ItemError(Exception):
    """An item the builder refuses to write."""


@dataclass
class Cursor:
    """Each stratum's cells in one seeded order, handed out round and round."""

    order: dict[str, list[dict[str, Any]]]
    pos: dict[str, int] = field(default_factory=dict)

    @classmethod
    def over(cls, cells: list[dict[str, Any]], rng: random.Random) -> "Cursor":
        order = {}
        for s in STRATA:
            mine = sorted((c for c in cells if c.get("stratum") == s), key=lambda c: str(c["bench_id"]))
            order[s] = rng.sample(mine, k=len(mine)) if mine else []
        return cls(order)

    def size(self, stratum: str) -> int:
        return len(self.order.get(stratum, []))

    def next(self, stratum: str) -> dict[str, Any] | None:
        mine = self.order.get(stratum, [])
        if not mine:
            return None
        i = self.pos.get(stratum, 0)
        self.pos[stratum] = i + 1
        return mine[i % len(mine)]


@dataclass
class Ctx:
    """What every generator reads: the reader, the drawable cells and the spec's radii."""

    reader: Reader
    cells: list[dict[str, Any]]
    nearby_radius_m: float = 5000.0
    label_radius_km: float = 25.0
    nearby_layers: tuple[str, ...] = ("em_conductors", "faults_250k")
    cursor: Cursor | None = None

    @property
    def bench_ids(self) -> set[str]:
        return {str(c["bench_id"]) for c in self.cells}

    def call(self, tool: str, cell: dict[str, Any], **kw: Any) -> T.ToolResult:
        return self.reader.call(tool, {"cell_id": str(cell["cell_id"]), **kw})

    @staticmethod
    def shown(tool: str, cell: dict[str, Any] | None, **kw: Any) -> dict[str, Any]:
        """The call as the model makes it: the cell by the id it is shown."""
        args = ({"cell_id": str(cell["bench_id"])} if cell is not None else {}) | kw
        return {"tool": tool, "args": args}

    def features(self, cell: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        r = self.call("cell_features", cell)
        return {str(row["feature"]): row for row in r.rows}, r.values

    def criteria(self, cell: dict[str, Any]) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
        r = self.call("criteria_breakdown", cell)
        return {str(row["criterion"]): row for row in r.rows}, r.values


# ---------------------------------------------------------------- gold


def _pick(values: dict[str, dict[str, Any]], ids: list[str]) -> dict[str, Any]:
    missing = [v for v in ids if v not in values]
    if missing:
        raise ItemError(f"gold cites ids the tool did not return: {missing}")
    return {v: values[v]["value"] for v in ids}


def gold_value(ids: list[str], values: dict[str, dict[str, Any]], unit: str | None = None,
               note: str = "") -> dict[str, Any]:
    return {"answer": "value", "value_ids": list(ids), "values": _pick(values, list(ids)), "unit": unit,
            "keys": None, "truth": None, "reason": None, "note": note}


def gold_set(keys: list[str], ids: list[str], values: dict[str, dict[str, Any]], note: str = "") -> dict[str, Any]:
    return {"answer": "set", "value_ids": list(ids), "values": _pick(values, list(ids)), "unit": None,
            "keys": sorted(keys), "truth": None, "reason": None, "note": note}


def gold_bool(truth: bool, ids: list[str], values: dict[str, dict[str, Any]], note: str = "") -> dict[str, Any]:
    return {"answer": "bool", "value_ids": list(ids), "values": _pick(values, list(ids)), "unit": None,
            "keys": None, "truth": bool(truth), "reason": None, "note": note}


def gold_abstain(reason: str, ids: list[str] | None = None, values: dict[str, dict[str, Any]] | None = None,
                 note: str = "") -> dict[str, Any]:
    if reason not in REASONS:
        raise ItemError(f"abstention reason {reason!r} is not one of {REASONS}")
    ids = list(ids or [])
    return {"answer": "abstain", "value_ids": ids, "values": _pick(values or {}, ids), "unit": None,
            "keys": None, "truth": None, "reason": reason, "note": note}


# ---------------------------------------------------------------- items


def check_wording(*texts: str | None) -> None:
    """No banned phrase and no real cell id in anything the model is shown."""
    for text in texts:
        if not text:
            continue
        low = text.lower()
        for phrase in BANNED:
            if phrase in low:
                raise ItemError(f"banned wording {phrase!r} in {text!r}")
        if P.CELL_ID.search(text):
            raise ItemError(f"a real cell id in model-facing text: {text!r}")


def make_item(tier: int, kind: str, cell: dict[str, Any] | None, question: str, tools: list[dict[str, Any]],
              gold: dict[str, Any], choice: dict[str, Any], stratum: str | None = None,
              premise: str | None = None, **extra: Any) -> dict[str, Any]:
    """One item, checked: the wording rule, the gold shape, and the bench-scheme ids."""
    check_wording(question, premise, gold.get("note"))
    if gold.get("answer") not in ANSWERS:
        raise ItemError(f"gold answer {gold.get('answer')!r} is not one of {ANSWERS}")
    cell_id = str(cell["cell_id"]) if cell else None
    bench_id = str(cell["bench_id"]) if cell else None
    item: dict[str, Any] = {
        "id": None, "tier": tier, "kind": kind,
        "stratum": stratum or (str(cell["stratum"]) if cell else GRID),
        "bench_id": bench_id, "cell_id": cell_id, "fold": (int(cell["fold"]) if cell and cell.get("fold") is not None else None),
        "question": question,
    }
    if premise is not None:
        item["premise"] = premise
    item |= {"tools": tools, "choice": choice, "gold": gold,
             "bench_value_ids": ([P.rewrite_id(v, cell_id, bench_id) for v in gold["value_ids"]]
                                 if cell_id and bench_id else None)}
    item |= extra
    return item


def dedup_key(kind: str, item: dict[str, Any]) -> str:
    return canonical_json([kind, item.get("bench_id"), item.get("choice")])


def draw(kind: str, n: int, make: Maker, ctx: Ctx, rng: random.Random, cell_based: bool = True,
         tier: int = 1) -> tuple[list[dict[str, Any]], int]:
    """Up to `n` items of one kind, and how many short. Cell-based kinds take an equal share per stratum;
    a stratum whose cells cannot yield more is left short rather than filled from another."""
    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    if not cell_based:
        misses = 0
        while len(out) < n and misses < 4 * max(n, 1):
            item = make(ctx, None, rng, None)
            if item is None or dedup_key(kind, item) in seen:
                misses += 1
                continue
            seen.add(dedup_key(kind, item))
            out.append(item)
        return out, n - len(out)
    cursor = ctx.cursor or Cursor.over(ctx.cells, rng)
    ctx.cursor = cursor
    base, rem = divmod(n, len(STRATA))
    for i, stratum in enumerate(STRATA):
        quota = base + (1 if i < rem else 0)
        made = misses = 0
        limit = 2 * cursor.size(stratum)
        while made < quota and misses < limit:
            cell = cursor.next(stratum)
            if cell is None:
                break
            item = make(ctx, cell, rng, None)
            if item is None or dedup_key(kind, item) in seen:
                misses += 1
                continue
            seen.add(dedup_key(kind, item))
            out.append(item)
            made += 1
            misses = 0
    return out, n - len(out)


def true_forms(val: dict[str, Any]) -> set[str]:
    """Every way the gate accepts this value written, so a corrupted token that lands on one is not a
    fabrication."""
    from ...prospect.memo import _formatted

    return set(_formatted(val))


def is_true(token: str, val: dict[str, Any]) -> bool:
    forms = true_forms(val)
    return token in forms or token.replace(",", "") in forms
