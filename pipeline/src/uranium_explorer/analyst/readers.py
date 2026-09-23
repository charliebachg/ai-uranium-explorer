"""The evidence readers (analyst v2) over real cells, for the dashboard.

The benchmark ran the readers on frozen, anonymised packs. Here the same design reads a real cell's pack
(`bench.pack.live_pack`: the same tools under the same switches, with the cell's own ids, because the panel
resolves every cited id against the live store) and its map card, and each result is kept whole: the four
readings with their summaries and claims, and the ranking call's answer. It is computed offline, as the
staged loop's dashboard chains are, and served as stored.

The run writes rows to its own directory first (`rows.jsonl`); `store_run` then lands them in
`agent.reading_run` in one short transaction, so a long model run never holds the store the service reads.

Two kinds of number are shown for a result that are not in the pack: the answer's probability and each
reading's strength. They are minted as values under `c:readers:<cell>:<run>:…`, so the panel prints them
through the same component as every other number, and a claim may be traced to the value it cites.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

from ..values import stat
from . import families as FAM
from .arms import ArmConfig

TABLE = "reading_run"
COLUMNS = ("reading_run_id", "cell_id", "run_id", "arm", "model", "verdict", "probability", "published",
           "problems_json", "answer_json", "readings_json", "values_json", "cost_usd", "duration_s", "created_at")
JSON_COLUMNS = ("problems_json", "answer_json", "readings_json", "values_json")


def value_base(cell_id: str, run_id: str) -> str:
    return f"c:readers:{cell_id}:{run_id}"


def _cited(claims: Any) -> list[str]:
    return [str(v) for c in (claims or []) if isinstance(c, dict) for v in (c.get("value_ids") or [])]


def record(cell_id: str, run_id: str, arm: ArmConfig, row: dict[str, Any], pack: dict[str, Any],
           created_at: str | None = None) -> dict[str, Any]:
    """One stored result from one `families.run_cell` row: the answer, the four readings whole, and every value
    they cite (from the pack) with the minted probability and strengths, so the row stands on its own."""
    answer = dict(row.get("answer") or {})
    readings = dict(row.get("readings") or {})
    base = value_base(cell_id, run_id)
    pack_values = pack.get("values") or {}
    values: dict[str, dict[str, Any]] = {}
    for vid in _cited(answer.get("claims")) + [v for r in readings.values() for v in _cited(r.get("claim_list"))]:
        if vid in pack_values:
            values[vid] = pack_values[vid]
    p = answer.get("probability")
    if isinstance(p, (int, float)) and not isinstance(p, bool):
        vid = f"{base}:probability"
        values[vid] = stat(vid, float(p), fmt="ratio3",
                           note=f"the evidence readers' probability for cell {cell_id} (run {run_id}): that the "
                                "public record labels it a known deposit or occurrence, a calibration figure, "
                                "never a probability that ore is present")
    for family, r in readings.items():
        s = r.get("strength")
        if isinstance(s, (int, float)) and not isinstance(s, bool):
            vid = f"{base}:{family}:strength"
            values[vid] = stat(vid, float(s), fmt="ratio3",
                               note=f"the {family} reader's strength for cell {cell_id}, from 0 to 1")
    return {
        "reading_run_id": f"{run_id}:{cell_id}", "cell_id": cell_id, "run_id": run_id, "arm": arm.name,
        "model": str(row.get("model_resolved") or arm.model), "verdict": answer.get("verdict"),
        "probability": float(p) if isinstance(p, (int, float)) and not isinstance(p, bool) else None,
        "published": bool(row.get("published")), "problems_json": list(row.get("problems") or []),
        "answer_json": answer,
        "readings_json": {f: {k: r.get(k) for k in ("assessment", "strength", "published", "attempts", "problems",
                                                     "summary", "claim_list", "unknowns")}
                          for f, r in readings.items()},
        "values_json": values, "cost_usd": float(row.get("cost_usd") or 0.0),
        "duration_s": float(row.get("duration_s") or 0.0),
        "created_at": created_at or dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
    }


def run_cells(cell_ids: list[str], arm: ArmConfig, inner: Callable[[ArmConfig], Any], budget_usd: float,
              out_dir: Path, log: Callable[[str], None] = print, workers: int = 3,
              card_paths: dict[str, Path] | None = None, packs: Callable[[str], dict[str, Any]] | None = None,
              cache_root: Path | None = None) -> dict[str, Any]:
    """Read each cell and append its record to `out_dir/rows.jsonl`; a cell already there is not read again,
    so a stopped run resumes. Returns the counts and what was spent."""
    from ..backends.cache import CachedBackend
    from ..bench.pack import live_pack
    from ..runtime.spend import BudgetExhausted, RunBudget

    if arm.agent != "v2":
        raise ValueError(f"arm {arm.name} is not an evidence readers (v2) arm")
    out_dir.mkdir(parents=True, exist_ok=True)
    rows_path = out_dir / "rows.jsonl"
    done = {json.loads(line)["cell_id"] for line in rows_path.read_text().splitlines() if line.strip()} \
        if rows_path.exists() else set()
    run_id = out_dir.name
    budget = RunBudget(cap_usd=budget_usd)
    backend = CachedBackend(inner(arm), root=cache_root, run_budget=budget, estimate_usd=arm.max_budget_usd_per_call)
    make_pack = packs or (lambda cid: live_pack(cid, arm.switches.keyed()))
    lock = threading.Lock()
    todo = [c for c in cell_ids if c not in done]
    failed: dict[str, str] = {}

    def one(cell_id: str) -> None:
        pack = make_pack(cell_id)
        row = FAM.run_cell(backend, pack, (card_paths or {}).get(cell_id), arm)
        rec = record(cell_id, run_id, arm, row, pack)
        with lock:
            with rows_path.open("a") as fh:
                fh.write(json.dumps(rec, default=str) + "\n")
        log(f"  {cell_id}: {rec['verdict']} p={rec['probability']} published={rec['published']} "
            f"${rec['cost_usd']:.2f}")

    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(one, c): c for c in todo}
        for fut in as_completed(futures):
            err = fut.exception()
            if err is not None:
                failed[futures[fut]] = f"{type(err).__name__}: {err}"
                log(f"  {futures[fut]}: failed: {failed[futures[fut]]}")
                if isinstance(err, BudgetExhausted):
                    for f in futures:
                        f.cancel()
    stored = sum(1 for line in rows_path.read_text().splitlines() if line.strip()) if rows_path.exists() else 0
    return {"run_id": run_id, "done": stored, "failed": failed, "skipped": len(done), "spent_usd": budget.spent_usd}


def store_run(con: Any, out_dir: Path) -> int:
    """Land a run's rows in `agent.reading_run`, replacing any row of the same run and cell."""
    import pandas as pd

    from ..store import append_frame

    rows = [json.loads(line) for line in (out_dir / "rows.jsonl").read_text().splitlines() if line.strip()]
    if not rows:
        return 0
    frame = pd.DataFrame([{c: (json.dumps(r.get(c), default=str) if c in JSON_COLUMNS else r.get(c))
                           for c in COLUMNS} for r in rows], columns=list(COLUMNS))
    con.begin()
    try:
        con.execute(f"delete from agent.{TABLE} where reading_run_id in ({','.join('?' * len(rows))})",
                    [r["reading_run_id"] for r in rows])
        append_frame(con, "agent", TABLE, frame, "agent")
        con.commit()
    except BaseException:
        con.rollback()
        raise
    return len(rows)


def for_cell(con: Any, cell_id: str) -> list[tuple[dict[str, Any], dict[str, Any]]]:
    """The cell's reader results, newest first, each as (the record the panel draws, the values it cites)."""
    try:
        cur = con.execute(f"select {', '.join(COLUMNS)} from agent.{TABLE} where cell_id = ? "
                          "order by created_at desc", [cell_id])
    except Exception:  # noqa: BLE001 - a store built before this table existed has no results, not an error
        return []
    out = []
    for raw in cur.fetchall():
        r = dict(zip(COLUMNS, raw, strict=True))
        for c in JSON_COLUMNS:
            r[c] = json.loads(r[c] or ("[]" if c == "problems_json" else "{}"))
        values = r.pop("values_json")
        base = value_base(r["cell_id"], r["run_id"])
        rec = {"reading_run_id": r["reading_run_id"], "run_id": r["run_id"], "arm": r["arm"], "model": r["model"],
               "verdict": r["verdict"], "published": bool(r["published"]), "problems": r["problems_json"],
               "answer": r["answer_json"] if r["published"] else {}, "created_at": r["created_at"],
               "cost_usd": r["cost_usd"],
               "probability_id": f"{base}:probability" if f"{base}:probability" in values else None,
               # a refused reading is shown as refused: its summary and claims failed the check, so neither is shown
               "readings": [{"family": f, **{k: v for k, v in rd.items() if k not in ("claim_list", "summary")},
                             "summary": (rd.get("summary") or "") if rd.get("published") else "",
                             "claims": (rd.get("claim_list") or []) if rd.get("published") else [],
                             "strength_id": f"{base}:{f}:strength" if f"{base}:{f}:strength" in values else None}
                            for f, rd in r["readings_json"].items()]}
        out.append((rec, values))
    return out


__all__ = ["COLUMNS", "TABLE", "for_cell", "record", "run_cells", "store_run", "value_base"]
