"""`ue bench recognise`: does the model recognise the ground from the report text it will read?

Anonymising text cuts a frontier model's recognition of the place only partway (in the literature, location
inferred from about 86% of raw texts and about 55% after anonymising), and the famous Athabasca deposits are
in every model's training data. A text arm that recognises a deposit and recalls what was found there would
look like a good reader. So before the arm reads benchmark v3's passages, the model is asked, cell by cell,
which area, property, deposit or camp the redacted passages describe, with the same model and effort the arm
uses and exactly the passages the fifth reader sees.

A guess counts as a recognition when a naming word in it (`pack.name_tokens`) matches one in a provincial label
within 10 km of the cell or in the property of a report file whose holes lie within 5 km. Recognised cells are
flagged; the pre-registration reports every result also on the cells nobody recognised.
"""

from __future__ import annotations

import json
import threading
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable

import numpy as np

from ..analyst import frozen as F
from ..backends.base import ExtractionRequest, UsageLimitReached
from ..ids import sha256_json, short

TASK = "bench_recognise"
SCHEMA_VERSION = "1.0.0"
PROMPT_VERSION = "bench/recognise/v1"
MODEL, EFFORT = "claude-opus-5", "medium"
SCHEMA: dict[str, Any] = {
    "type": "object", "additionalProperties": False, "required": ["recognised", "name", "confidence", "basis"],
    "properties": {"recognised": {"type": "boolean"}, "name": {"type": "string"},
                   "confidence": {"type": "number", "minimum": 0, "maximum": 1}, "basis": {"type": "string"}},
}
SYSTEM = """You are shown passages from mineral exploration assessment reports about one small area. Names,
numbers and outcomes were removed from them. Say whether you recognise which area, property, deposit, prospect or
camp the passages describe. If you do, give its name as specifically as you can; if you do not, answer
recognised false and name "unknown". confidence is how sure you are of the name, from 0 to 1. basis is one
sentence on what in the passages gave it away, or "nothing"."""


def request(bench_id: str, passages_text: str, stage: Path) -> ExtractionRequest:
    stage.mkdir(parents=True, exist_ok=True)
    path = stage / "passages.md"
    path.write_text(passages_text)
    return ExtractionRequest(
        task=TASK, images=(), stage_files=((path, "passages.md"),), system_prompt=SYSTEM,
        user_prompt=f"Cell {bench_id}. Read {{STAGE_DIR}}/passages.md, then answer once, as JSON.",
        schema=SCHEMA, schema_version=SCHEMA_VERSION, prompt_version=PROMPT_VERSION, model=MODEL, effort=EFFORT,
        context_hash=short(sha256_json({"bench_id": bench_id, "task": TASK})))


def truth_names(cell_id: str, con: Any, holes: Any = None, label_km: float = 10.0, file_km: float = 5.0
                ) -> set[str]:
    """The naming words a correct guess for this cell could contain: labels within `label_km`, the properties
    and companies of report files whose holes lie within `file_km`."""
    from .celltext import hole_table
    from .pack import company_names, name_tokens

    cx, cy = con.execute("select cx, cy from derived.cell where cell_id = ?", [cell_id]).fetchone()
    out: set[str] = set()
    for (name,) in con.execute(
            "select distinct l.label_name from derived.cell_label l join derived.cell c using (cell_id) "
            "where l.label_name is not null and sqrt(power(c.cx - ?, 2) + power(c.cy - ?, 2)) <= ?",
            [cx, cy, label_km * 1000]).fetchall():
        out |= name_tokens(str(name))
    holes = hole_table() if holes is None else holes
    d = np.hypot(holes["x"].to_numpy() - cx, holes["y"].to_numpy() - cy)
    files = sorted(set(holes["file"][d <= file_km * 1000]))
    if files:
        for prop, company in con.execute(
                "select property, company from native.corpus_file where file_num in ("
                + ",".join("?" * len(files)) + ")", files).fetchall():
            if prop:
                out |= name_tokens(str(prop).title())
            for n in company_names(company):
                out |= name_tokens(str(n).title())
    return {w.lower() for w in out} - REGIONAL


#: the basin, the province and the country are what every cell has in common; naming them recognises nothing
REGIONAL = frozenset({"athabasca", "saskatchewan", "canada", "canadian", "basin", "northern", "wollaston"})


def matches(guess: str, truth: set[str]) -> list[str]:
    from .pack import name_tokens

    return sorted({w.lower() for w in name_tokens(str(guess).title())} & truth)


def run(version: str, inner: Any, budget_usd: float, out_path: Path, workers: int = 3,
        cells: list[str] | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Ask for every open cell with passages; append each answer to `out_path` (resumable); score the lot."""
    import tempfile

    from ..analyst.families import render_report_passages
    from ..backends.cache import CachedBackend
    from ..runtime.spend import BudgetExhausted, RunBudget
    from ..store import connect
    from .celltext import hole_table

    bench = F.load_bench(version)
    todo = [c for c in bench.open_cells() if bench.passages(c["bench_id"]) and (not cells or c["bench_id"] in cells)]
    done = {json.loads(line)["bench_id"] for line in out_path.read_text().splitlines() if line.strip()} \
        if out_path.is_file() else set()
    backend = CachedBackend(inner, run_budget=RunBudget(cap_usd=budget_usd), estimate_usd=0.10)
    lock = threading.Lock()
    out_path.parent.mkdir(parents=True, exist_ok=True)
    stage_root = Path(tempfile.mkdtemp(prefix="ue_recognise_"))

    def one(cell: dict[str, Any]) -> None:
        bid = str(cell["bench_id"])
        text = render_report_passages(bid, bench.passages(bid))
        resp = backend.call(request(bid, text, stage_root / bid))
        row = {"bench_id": bid, "answer": dict(resp.structured or {}), "cost_usd": float(resp.cost_usd or 0.0),
               "from_cache": bool(getattr(resp, "from_cache", False))}
        with lock, out_path.open("a") as fh:
            fh.write(json.dumps(row) + "\n")
        a = row["answer"]
        log(f"  {bid}: {a.get('recognised')} {a.get('name')!r} {a.get('confidence')}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = [pool.submit(one, c) for c in todo if c["bench_id"] not in done]
        for f in futures:
            err = f.exception()
            if isinstance(err, (BudgetExhausted, UsageLimitReached)):
                pool.shutdown(wait=False, cancel_futures=True)
                raise err
            if err is not None:
                log(f"  failed: {type(err).__name__}: {err}")
    con = connect(read_only=True)
    try:
        return score(bench, out_path, con, hole_table())
    finally:
        con.close()


def score(bench: F.Bench, out_path: Path, con: Any, holes: Any) -> dict[str, Any]:
    """Per stratum: how many cells the model said it recognised, and how many it named correctly."""
    rows = [json.loads(line) for line in out_path.read_text().splitlines() if line.strip()]
    cell_of = {c["bench_id"]: c["cell_id"] for c in bench.cells}
    by: dict[str, dict[str, int]] = {}
    flagged = []
    for r in rows:
        stratum = str(bench.key.get(r["bench_id"], {}).get("stratum"))
        a = r.get("answer") or {}
        hit = matches(str(a.get("name") or ""), truth_names(cell_of[r["bench_id"]], con, holes)) \
            if a.get("recognised") else []
        s = by.setdefault(stratum, {"cells": 0, "said_recognised": 0, "named_correctly": 0})
        s["cells"] += 1
        s["said_recognised"] += bool(a.get("recognised"))
        s["named_correctly"] += bool(hit)
        if hit:
            flagged.append({"bench_id": r["bench_id"], "stratum": stratum, "name": a.get("name"), "matched": hit,
                            "confidence": a.get("confidence")})
    return {"cells": len(rows), "by_stratum": by, "recognised": flagged,
            "cost_usd": sum(float(r.get("cost_usd") or 0.0) for r in rows)}


__all__ = ["matches", "request", "run", "score", "truth_names"]
