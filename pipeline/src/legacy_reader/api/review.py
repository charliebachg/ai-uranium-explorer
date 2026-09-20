"""The review queue over HTTP (PRD §8.2 stage 4, §A.2): what the second family disagreed on, and who decided.

Two routes. `GET /api/review` lists items (open by default), paged and filterable by file, for the dashboard's
review page; anyone who can reach the API may look. `POST /api/review/{queue_id}` records a decision and
needs the geologist role (`api.auth`), because a resolution is a person's statement about a reading and the
row says whose. Neither route rewrites a reading: the decision lands on the queue row beside the two readings
it chose between, and `read.field_value` is never touched (`extractor.queue.resolve`).

The store connection is the serving process's own read-write one (`LR_STORE_RW=1`), opened per request the
way the conversation persistence does.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fastapi import APIRouter, Depends, HTTPException, Query
from pydantic import BaseModel, Field

from ..extractor import queue as Q
from ..store import connect
from . import auth as AUTH


class Reading(BaseModel):
    """One reader's view of a value, as the comparer recorded it. Open on purpose: the agreement rule may
    add a field and the page should still parse."""

    model_config = {"extra": "allow"}
    field: str
    scope: str
    as_printed: str
    unit_as_printed: str | None = None
    analyte: str | None = None
    quote: str | None = None
    bbox: list[float] | None = None
    row_index: int | None = None
    value_id: str | None = None
    model: str | None = None


class ReviewItem(BaseModel):
    queue_id: str
    file_num: str
    page: int
    page_id: str | None = None
    field: str
    field_type: str | None = None
    value_id: str | None = None
    reading_a: Reading | None = None
    reading_b: Reading | None = None
    reason: str
    status: str
    run_id: str
    model_a: str | None = None
    model_b: str | None = None
    created_at: str
    resolved_by: str | None = None
    resolved_at: str | None = None
    resolution: dict[str, Any] | None = None


class ReviewPage(BaseModel):
    items: list[ReviewItem]
    total: int
    limit: int
    offset: int
    status: str
    file_num: str | None = None
    counts: dict[str, int] = Field(default_factory=dict)


class Decision(BaseModel):
    decision: str = Field(pattern="^(accepted_a|accepted_b|rejected|edited)$")
    value: dict[str, Any] | None = Field(default=None, description="for `edited`: the reading the person keyed, with `as_printed`")
    note: str = Field(default="", max_length=2000)


def build_router(db_path: Path | None = None) -> APIRouter:
    router = APIRouter(tags=["review"])

    @router.get("/api/review", response_model=ReviewPage)
    def list_review(file: str | None = Query(None, description="an assessment file number"),
                    status: str = Query("open", pattern="^(open|accepted_a|accepted_b|rejected|edited|all)$"),
                    limit: int = Query(50, ge=1, le=500), offset: int = Query(0, ge=0)) -> dict[str, Any]:
        con = connect(db_path, read_only=True)
        try:
            items, total = Q.list_items(con, file_num=file, status=status, limit=limit, offset=offset)
            counts = Q.counts(con, file_num=file)
        finally:
            con.close()
        return {"items": items, "total": total, "limit": limit, "offset": offset, "status": status,
                "file_num": file, "counts": counts}

    @router.post("/api/review/{queue_id}", response_model=ReviewItem)
    def resolve_review(queue_id: str, body: Decision,
                       who: AUTH.Principal = Depends(AUTH.require("geologist"))) -> dict[str, Any]:
        con = connect(db_path)
        try:
            try:
                return Q.resolve(con, queue_id, body.decision, resolved_by=who.name, value=body.value, note=body.note)
            except KeyError:
                raise HTTPException(404, "no such queue item")
            except PermissionError as err:
                raise HTTPException(409, str(err))
            except ValueError as err:
                raise HTTPException(422, str(err))
        finally:
            con.close()

    return router
