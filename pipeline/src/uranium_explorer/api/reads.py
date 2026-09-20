"""Serving reads: the candidate list through PostGIS when the serving database is there, and a per-cell
evidence cache keyed to the analytics store's version.

An evidence record is four tool calls over DuckDB, about 140 ms; a rail that re-reads it on every click pays
that every time although nothing changed. The cache key is the store file's modification time, so any write
to the store (a new score run, a memo, a conversation turn) invalidates every cached record at once. That is
coarser than it needs to be and exactly as safe as it needs to be: a stale evidence record is a wrong number.
"""

from __future__ import annotations

import os
import threading
import time
from collections import OrderedDict
from typing import Any, Callable

from ..prospect import serve as S
from ..store import db_path

EVIDENCE_CACHE_SIZE = 512
PG_RETRY_S = 60.0


def store_stamp() -> float:
    """The analytics store's version, as far as a cache needs one: its file's modification time."""
    p = db_path()
    try:
        return p.stat().st_mtime_ns / 1e9
    except FileNotFoundError:
        return 0.0


class EvidenceCache:
    def __init__(self, compute: Callable[[str], dict[str, Any]] | None = None, size: int = EVIDENCE_CACHE_SIZE,
                 stamp: Callable[[], float] = store_stamp) -> None:
        # resolved at call time, not at definition time, so a test that patches serve.evidence is honoured
        self.compute = compute or (lambda cid: S.evidence(cid))
        self.size, self.stamp = size, stamp
        self._items: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self._stamp = -1.0
        self._lock = threading.Lock()
        self._inflight: dict[str, threading.Lock] = {}   # one computation per cell at a time; the rest wait for it
        self.hits = 0
        self.misses = 0

    def _lookup(self, cell_id: str) -> dict[str, Any] | None:
        now = self.stamp()
        with self._lock:
            if now != self._stamp:
                self._items.clear()
                self._stamp = now
            hit = self._items.get(cell_id)
            if hit is not None:
                self._items.move_to_end(cell_id)
            return hit

    def get(self, cell_id: str) -> dict[str, Any]:
        hit = self._lookup(cell_id)
        if hit is not None:
            self.hits += 1
            return hit
        with self._lock:
            gate = self._inflight.setdefault(cell_id, threading.Lock())
        with gate:  # ten readers asking for one cold cell cost one computation, not ten
            hit = self._lookup(cell_id)
            if hit is not None:
                self.hits += 1
                return hit
            record = self.compute(cell_id)
            with self._lock:
                if self.stamp() == self._stamp:
                    self._items[cell_id] = record
                    if len(self._items) > self.size:
                        self._items.popitem(last=False)
                self.misses += 1
                self._inflight.pop(cell_id, None)
        return record

    def warm(self, cell_ids: list[str], log: Callable[[str], None] = lambda _m: None) -> threading.Thread:
        """Compute the given cells in the background, so the ranked list is answered from memory."""
        def run() -> None:
            t0 = time.perf_counter()
            for cid in cell_ids:
                try:
                    self.get(cid)
                except Exception as err:  # noqa: BLE001 - a warm-up failure is not a request failure, but it is said
                    log(f"  evidence warm-up stopped at {cid}: {type(err).__name__}: {err}")
                    return
            log(f"  evidence warm-up: {len(cell_ids)} cells in {time.perf_counter() - t0:.1f} s")
        th = threading.Thread(target=run, name="ue-evidence-warm", daemon=True)
        th.start()
        return th


def candidates_pg(limit: int, model: str, dsn: str) -> list[dict[str, Any]]:
    """The same ranking the DuckDB query gives, with the nearest-label distance from PostGIS."""
    from ..store.pg import connect_pg

    conn = connect_pg(dsn)
    try:
        with conn.cursor() as cur:
            cur.execute(
                """
                with lab as (select c.geom from derived.cell_label l join derived.cell c using (cell_id)
                             where l.label_tier <> 'unlabelled'),
                     top as (select s.cell_id, s.score, s.known_share, c.geom, c.lon, c.lat, c.in_basin,
                                    l.label_tier, l.label_name
                             from derived.cell_score s
                             join derived.cell c using (cell_id)
                             join derived.cell_label l using (cell_id)
                             where s.model = %s and s.score is not null
                             order by s.score desc limit %s)
                select top.cell_id, round(top.score::numeric, 4)::float, round(top.known_share::numeric, 3)::float,
                       top.lon, top.lat, top.in_basin, top.label_tier, top.label_name,
                       round((near.d / 1000)::numeric, 2)::float
                from top
                cross join lateral (select ST_Distance(top.geom, lab.geom) as d from lab
                                    order by top.geom <-> lab.geom limit 1) as near
                order by top.score desc
                """,
                [model, limit],
            )
            rows = cur.fetchall()
    finally:
        conn.close()
    keys = ("cell_id", "score", "known_share", "lon", "lat", "in_basin", "label_tier", "label_name", "km_to_label")
    return [dict(zip(keys, r, strict=True)) for r in rows]


class Candidates:
    """PostGIS when UE_PG_DSN is set and answers; DuckDB otherwise, and again after a failure, retried later."""

    def __init__(self, dsn: str | None = None, duck: Callable[[int, str], list[dict[str, Any]]] | None = None,
                 pg: Callable[[int, str, str], list[dict[str, Any]]] | None = None) -> None:
        self.dsn = dsn if dsn is not None else os.environ.get("UE_PG_DSN", "").strip()
        self.duck = duck or (lambda limit, model: S.candidates(limit, model))
        self.pg = pg or candidates_pg
        self.failed_at = 0.0
        self.source = "duckdb"

    def get(self, limit: int, model: str) -> list[dict[str, Any]]:
        if self.dsn and time.monotonic() - self.failed_at > PG_RETRY_S:
            try:
                rows = self.pg(limit, model, self.dsn)
                self.source = "postgis"
                return rows
            except Exception:  # noqa: BLE001 - the serving database being down is a state, not a crash
                self.failed_at = time.monotonic()
        self.source = "duckdb"
        return self.duck(limit, model)
