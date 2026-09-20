"""The review queue and the agreement marks, in the `read` tier (PRD §8.2, stage 4; §A.2's memo review queue).

Two tables, both filed beside the values they are about and never rewriting them:

* `read.agreement`    one row per value either reader found on a compared page: agreed, disagreed, or found
                      by one reader only, with both readings as JSON and the first reading's value id when
                      the store holds it. A consumer that wants only agreed values joins on `value_id`.
* `read.review_item`  the queue: every disagreement and every value only one reader found, open until a
                      person resolves it. A resolution is recorded on the row (`status`, `resolved_by`,
                      `resolved_at`, `resolution_json` carrying the accepted reading); the two readings on
                      the row and the original `read.field_value` rows are left as they were, so the
                      decision can always be seen beside what it decided between.

Rows are keyed by a hash of what they are about (run, page, field, the value id or the second reader's
value), so filing the same comparison twice is a no-op rather than a duplicate.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from ..ids import sha256_json, short
from ..store import connect
from .agree import Pair

STATUSES = ("open", "accepted_a", "accepted_b", "rejected", "edited")
DECISIONS = tuple(s for s in STATUSES if s != "open")


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def _json(obj: Any) -> str | None:
    return None if obj is None else json.dumps(obj, separators=(",", ":"), default=str)


def _candidate_key(pair: Pair) -> list[Any]:
    """What a row is about: the first reading's value id when there is one, else the second reading's
    field, printed value and box (the only identity a value that was never filed has)."""
    if pair.a is not None:
        return [pair.a.value_id or [pair.a.field, pair.a.as_printed, pair.a.bbox, pair.a.row_index]]
    assert pair.b is not None
    return [pair.b.field, pair.b.as_printed, pair.b.bbox, pair.b.row_index]


def agreement_row(pair: Pair, *, run_id: str, file_num: str, page_id: str, page_no: int,
                  model_a: str | None, model_b: str | None, prompt_version: str | None) -> dict[str, Any]:
    return {
        "agreement_id": short(sha256_json([run_id, page_id, pair.field, _candidate_key(pair)]), 16),
        "file_num": file_num, "page": int(page_no), "page_id": page_id,
        "field": pair.field, "field_type": pair.field_type, "status": pair.status,
        "matched_by": pair.matched_by, "detail": pair.detail,
        "value_id": pair.a.value_id if pair.a else None,
        "reading_a_json": _json(pair.a.as_dict() if pair.a else None),
        "reading_b_json": _json(pair.b.as_dict() if pair.b else None),
        "model_a": model_a, "model_b": model_b, "prompt_version": prompt_version,
        "run_id": run_id, "compared_at": _now(),
    }


def queue_row(pair: Pair, *, run_id: str, file_num: str, page_id: str, page_no: int,
              model_a: str | None, model_b: str | None) -> dict[str, Any]:
    if pair.status == "agreed":
        raise ValueError("an agreed value does not go to the queue")
    return {
        "queue_id": short(sha256_json(["queue", page_id, pair.field, _candidate_key(pair)]), 16),
        "file_num": file_num, "page": int(page_no), "page_id": page_id,
        "field": pair.field, "field_type": pair.field_type,
        "value_id": pair.a.value_id if pair.a else None,
        "reading_a_json": _json(pair.a.as_dict() if pair.a else None),
        "reading_b_json": _json(pair.b.as_dict() if pair.b else None),
        "reason": pair.status, "status": "open", "run_id": run_id,
        "model_a": model_a, "model_b": model_b, "created_at": _now(),
        "resolved_by": None, "resolved_at": None, "resolution_json": None,
    }


# --------------------------------------------------------------------------------- writing

_AGREEMENT_COLS = ("agreement_id", "file_num", "page", "page_id", "field", "field_type", "status", "matched_by",
                   "detail", "value_id", "reading_a_json", "reading_b_json", "model_a", "model_b", "prompt_version",
                   "run_id", "compared_at")
_QUEUE_COLS = ("queue_id", "file_num", "page", "page_id", "field", "field_type", "value_id", "reading_a_json",
               "reading_b_json", "reason", "status", "run_id", "model_a", "model_b", "created_at", "resolved_by",
               "resolved_at", "resolution_json")


def _insert_new(con: Any, table: str, cols: tuple[str, ...], key: str, rows: list[dict[str, Any]]) -> int:
    """Insert the rows whose key is not there yet. Returns how many landed."""
    n = 0
    marks = ", ".join("?" * len(cols))
    for r in rows:
        exists = con.execute(f"select 1 from {table} where {key} = ?", [r[key]]).fetchone()
        if exists:
            continue
        con.execute(f"insert into {table} ({', '.join(cols)}) values ({marks})", [r[c] for c in cols])
        n += 1
    return n


def file_rows(con: Any, agreement: list[dict[str, Any]], queue: list[dict[str, Any]]) -> dict[str, int]:
    """File a page's comparison: the agreement marks, then the queue rows. Idempotent by key."""
    return {"agreement": _insert_new(con, "read.agreement", _AGREEMENT_COLS, "agreement_id", agreement),
            "queue": _insert_new(con, "read.review_item", _QUEUE_COLS, "queue_id", queue)}


# --------------------------------------------------------------------------------- reading and resolving

def _item(row: tuple[Any, ...]) -> dict[str, Any]:
    d = dict(zip(_QUEUE_COLS, row, strict=True))
    for k in ("reading_a_json", "reading_b_json", "resolution_json"):
        d[k[:-5]] = json.loads(d.pop(k)) if d.get(k) else None
    return d


def list_items(con: Any, file_num: str | None = None, status: str = "open", limit: int = 50,
               offset: int = 0) -> tuple[list[dict[str, Any]], int]:
    """A page of queue items and the total that match, oldest first so a queue is worked in order."""
    where, args = ["1 = 1"], []
    if status and status != "all":
        where.append("status = ?")
        args.append(status)
    if file_num:
        where.append("file_num = ?")
        args.append(file_num)
    clause = " and ".join(where)
    total = con.execute(f"select count(*) from read.review_item where {clause}", args).fetchone()[0]
    rows = con.execute(
        f"select {', '.join(_QUEUE_COLS)} from read.review_item where {clause} "
        f"order by created_at, file_num, page, field, queue_id limit ? offset ?", [*args, int(limit), int(offset)]
    ).fetchall()
    return [_item(r) for r in rows], int(total)


def get_item(con: Any, queue_id: str) -> dict[str, Any] | None:
    row = con.execute(f"select {', '.join(_QUEUE_COLS)} from read.review_item where queue_id = ?", [queue_id]).fetchone()
    return _item(row) if row else None


def resolve(con: Any, queue_id: str, decision: str, resolved_by: str, value: dict[str, Any] | None = None,
            note: str = "") -> dict[str, Any]:
    """Record a person's decision on one open item. `accepted_a` and `accepted_b` file that reading as the
    accepted one; `edited` files the reading the person typed (`value`); `rejected` files neither. The
    readings on the row are not touched, and neither is any `read.field_value` row."""
    if decision not in DECISIONS:
        raise ValueError(f"decision must be one of {DECISIONS}, not {decision!r}")
    item = get_item(con, queue_id)
    if item is None:
        raise KeyError(queue_id)
    if item["status"] != "open":
        raise PermissionError(f"{queue_id} was already resolved ({item['status']} by {item['resolved_by']})")
    if decision == "accepted_a" and item["reading_a"] is None:
        raise ValueError("there is no first reading to accept on this item")
    if decision == "accepted_b" and item["reading_b"] is None:
        raise ValueError("there is no second reading to accept on this item")
    if decision == "edited" and not (value and value.get("as_printed")):
        raise ValueError("an edited resolution needs the value as printed")
    accepted = {"accepted_a": item["reading_a"], "accepted_b": item["reading_b"], "edited": value,
                "rejected": None}[decision]
    resolution = {"decision": decision, "reading": accepted, "note": note, "resolved_by": resolved_by,
                  "resolved_at": _now()}
    con.execute("update read.review_item set status = ?, resolved_by = ?, resolved_at = ?, resolution_json = ? "
                "where queue_id = ?", [decision, resolved_by, resolution["resolved_at"], _json(resolution), queue_id])
    out = get_item(con, queue_id)
    assert out is not None
    return out


def counts(con: Any, file_num: str | None = None) -> dict[str, int]:
    where, args = ("where file_num = ?", [file_num]) if file_num else ("", [])
    rows = con.execute(f"select status, count(*) from read.review_item {where} group by 1", args).fetchall()
    return {str(s): int(n) for s, n in rows}


def open_store(path: Path | None) -> Any:
    """A read-write connection to the store the queue is filed in; the schema is applied on the way in."""
    return connect(path)
