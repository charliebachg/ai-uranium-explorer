"""How the generators read the store: the deterministic tools, with the benchmark session's rules applied.

The gold of an interface item has to be what the agent's own tools will return when it is asked the question,
or the benchmark measures the difference between two readers rather than the agent. A benchmark run opens a
blinded session over the cell (`analyst.session`), and three of that session's rules change what a tool
returns, so the same three are applied here:

* **B30** — `label_context` is asked with the evaluated cell masked, and a row at 0.0 km is dropped;
* **B18** — `cell_scores` serves the out-of-fold scores for the cell's fold, never the served table that saw
  every label; the scores come from the analyst benchmark's frozen `oof_scores.csv` (its own record, hashed
  in its manifest) and from the store's `derived.cell_score_oof` only when that file is absent;
* **B17** — a file is only ever named from the cell's frozen blind-list.

Every call is memoised on its arguments, so a build reads each tool result once and the audit re-reads it the
same way. A `Reader` is a protocol so the tests can hand the generators hand-built tool results and never
touch a store.
"""

from __future__ import annotations

from typing import Any, Callable, Mapping, Protocol

import pandas as pd

from ...ids import canonical_json
from ...prospect import tools as T
from .. import pack as P

NO_SUCH_CELL = "no such cell"


class Reader(Protocol):
    def call(self, tool: str, args: dict[str, Any]) -> T.ToolResult: ...

    def blind(self, bench_id: str) -> list[str]: ...


def cited(row: Mapping[str, Any]) -> set[str]:
    """The value ids a tool row cites: every `*_id` string it carries."""
    return {v for k, v in row.items() if k.endswith("_id") and isinstance(v, str)}


def drop_rows(result: T.ToolResult, doomed: Callable[[dict[str, Any]], bool]) -> T.ToolResult:
    """The result without the rows `doomed` names and without the values only those rows cite."""
    keep = [r for r in result.rows if not doomed(r)]
    gone: set[str] = set()
    for r in result.rows:
        if doomed(r):
            gone |= cited(r)
    values = {k: v for k, v in result.values.items() if k not in gone}
    return T.ToolResult(result.tool, result.args, keep, values, result.note)


def exists(reader: Reader, cell_id: str) -> bool:
    """Whether the grid has this cell, by asking a tool that says so rather than by reading a table."""
    return reader.call("label_context", {"cell_id": cell_id}).note != NO_SUCH_CELL


class StoreReader:
    """The live tools over the store, read-only, with the session rules above and a memo per call."""

    def __init__(self, cells: list[dict[str, Any]], oof: pd.DataFrame | None = None,
                 blind: Mapping[str, list[str]] | None = None,
                 tools: Mapping[str, Callable[..., T.ToolResult]] | None = None) -> None:
        self.fold = {str(c["cell_id"]): int(c["fold"]) for c in cells if c.get("fold") is not None}
        self.by_bench = {str(c["bench_id"]): str(c["cell_id"]) for c in cells}
        self.oof = oof
        self.blind_lists = {str(k): [str(f) for f in v] for k, v in (blind or {}).items()}
        self.tools = tools if tools is not None else T.REGISTRY
        self._memo: dict[tuple[str, str], T.ToolResult] = {}

    def call(self, tool: str, args: dict[str, Any]) -> T.ToolResult:
        key = (tool, canonical_json(args))
        hit = self._memo.get(key)
        if hit is not None:
            return hit
        result = self._serve(tool, dict(args))
        self._memo[key] = result
        return result

    def blind(self, bench_id: str) -> list[str]:
        return list(self.blind_lists.get(bench_id, []))

    def _serve(self, tool: str, kw: dict[str, Any]) -> T.ToolResult:
        if tool == "cell_scores":
            return self._oof_scores(str(kw["cell_id"]))
        fn = self.tools.get(tool)
        if fn is None:
            raise T.ToolError(f"no tool named {tool!r}; available: {', '.join(sorted(self.tools))}")
        if tool == "label_context":
            kw["mask_cell"] = kw["cell_id"]
        result = fn(**kw)
        if tool == "label_context":
            result = drop_rows(result, lambda r: r.get("distance_km") == 0.0)
        return result

    def _oof_scores(self, cell_id: str) -> T.ToolResult:
        """B18: the out-of-fold rows of this cell's fold, in the served tool's shape and id scheme."""
        fold = self.fold.get(cell_id)
        if fold is None:
            return T.ToolResult("cell_scores", {"cell_id": cell_id}, note=NO_SUCH_CELL)
        if self.oof is not None:
            served = P.oof_result(cell_id, oof=self.oof)
        else:
            from ...store import connect

            con = connect(read_only=True)
            try:
                served = P.oof_result(cell_id, con=con)
            finally:
                con.close()
        return drop_rows(served, lambda r: r.get("fold") != fold)


def frozen_oof(csv: Any, cells: list[dict[str, Any]]) -> pd.DataFrame:
    """The analyst benchmark's `oof_scores.csv` (keyed by bench id) as the frame `pack.oof_result` reads
    (keyed by cell id)."""
    df = pd.read_csv(csv)
    ids = pd.DataFrame([{"bench_id": c["bench_id"], "cell_id": c["cell_id"]} for c in cells])
    out = df.merge(ids, on="bench_id", how="inner")
    return out[["cell_id", "model", "fold_kind", "fold", "score"]]
