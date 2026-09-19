"""`lr serve`: a small local service so the web app can ask the agent about a cell.

The published site is static files, which is right for everything precomputed: scores, features, memos. A
conversation is not precomputed, so it needs something that can call the model. This is that, and nothing more
— it runs on localhost, it is not an authenticated public API, and the public-safe build simply does not offer
the chat.

Every endpoint reads the same evidence record the map and the panels are drawn from, so the three views cannot
disagree with each other. No endpoint invents a number: `/api/chat` answers are passed through the same gate
that guards a published memo.
"""

from __future__ import annotations

import json
from typing import Any, Callable

from ..store import connect
from . import tools as T

DEFAULT_PORT = 8787



def candidates(limit: int = 40, model: str = "criteria") -> list[dict[str, Any]]:
    """Cells worth looking at, with the context that says whether the score means anything."""
    con = connect(read_only=True)
    try:
        rows = con.execute(
            """
            with lab as (
              select c.lon, c.lat from derived.cell_label l join derived.cell c using (cell_id)
              where l.label_tier <> 'unlabelled'
            )
            select s.cell_id, round(s.score, 4) as score, round(s.known_share, 3) as known_share,
                   c.lon, c.lat, c.in_basin, l.label_tier, l.label_name,
                   round(min(6371.0 * 2 * asin(sqrt(
                     pow(sin(radians(c.lat - lab.lat) / 2), 2) +
                     cos(radians(lab.lat)) * cos(radians(c.lat)) *
                     pow(sin(radians(c.lon - lab.lon) / 2), 2)))), 2) as km_to_label
            from derived.cell_score s
            join derived.cell c using (cell_id)
            join derived.cell_label l using (cell_id), lab
            where s.model = ? and s.score is not null
            group by s.cell_id, s.score, s.known_share, c.lon, c.lat, c.in_basin, l.label_tier, l.label_name
            order by 2 desc limit ?
            """,
            [model, limit],
        ).fetchall()
    finally:
        con.close()
    return [
        {"cell_id": r[0], "score": r[1], "known_share": r[2], "lon": r[3], "lat": r[4],
         "in_basin": bool(r[5]), "label_tier": r[6], "label_name": r[7], "km_to_label": r[8]}
        for r in rows
    ]


def evidence(cell_id: str) -> dict[str, Any]:
    """The whole evidence record for one cell: exactly what the chat agent is given."""
    parts = {name: T.call(name, {"cell_id": cell_id}).as_json()
             for name in ("cell_scores", "cell_features", "criteria_breakdown", "label_context")}
    values: dict[str, Any] = {}
    for part in parts.values():
        values |= part.get("values") or {}
    con = connect(read_only=True)
    try:
        memos = con.execute(
            "select memo_id, role, verdict, published, created_at from agent.memo "
            "where cell_id = ? order by created_at desc, role", [cell_id]
        ).fetchall()
        claims = con.execute(
            "select c.memo_id, c.claim_no, c.text, c.value_ids from agent.memo_claim c "
            "join agent.memo m using (memo_id) where m.cell_id = ? order by c.memo_id, c.claim_no",
            [cell_id],
        ).fetchall()
        cell = con.execute(
            "select lon, lat, in_basin from derived.cell where cell_id = ?", [cell_id]
        ).fetchone()
    finally:
        con.close()
    by_memo: dict[str, list[dict[str, Any]]] = {}
    for memo_id, claim_no, text, value_ids in claims:
        by_memo.setdefault(memo_id, []).append(
            {"claim_no": claim_no, "text": text, "value_ids": json.loads(value_ids or "[]")}
        )
    return {
        "cell_id": cell_id,
        "lon": cell[0] if cell else None,
        "lat": cell[1] if cell else None,
        "in_basin": bool(cell[2]) if cell else None,
        "parts": parts,
        "values": values,
        "memos": [
            {"memo_id": m[0], "role": m[1], "verdict": m[2], "published": bool(m[3]), "created_at": m[4],
             "claims": by_memo.get(m[0], [])}
            for m in memos
        ],
    }


def make_backend(kind: str = "openai", model: str = "") -> tuple[Any, str]:
    """Pick the backend the chat runs on, and the model name that goes with it.

    Only the chat moved to OpenAI. Reading pages, the memo panel and the evals stay on Claude Code, and the
    two caches never mix because the cache key carries the backend family.
    """
    from ..backends.cache import CachedBackend

    if kind == "claude":
        from ..backends.claude_cli import ClaudeCliBackend

        return CachedBackend(ClaudeCliBackend(timeout_s=600, max_budget_usd=1.20)), model or "claude-sonnet-5"

    if kind != "openai":
        raise ValueError(f"unknown backend {kind!r}: use openai or claude")
    from ..backends.openai_api import OpenAIBackend, model_name

    return CachedBackend(OpenAIBackend()), model or model_name()


def serve(port: int = DEFAULT_PORT, model: str = "", effort: str = "medium",
          backend: str = "openai", log: Callable[[str], None] = print, host: str = "127.0.0.1",
          web_dist: str | None = None) -> None:
    """Run the FastAPI service on localhost. The routes and the NDJSON events are unchanged from the stdlib
    server this replaced; conversations are now persisted turn by turn in the agent tier."""
    import os

    import uvicorn

    from ..api.app import create_app

    os.environ["LR_STORE_RW"] = "1"   # one connection mode for the whole process; see store.one_mode

    chosen, model = make_backend(backend, model)
    from pathlib import Path

    dist = Path(web_dist) if web_dist else None
    app = create_app(lambda: chosen, model, effort, backend_name=backend, web_dist=dist, warm=True)
    log(f"  listening on http://{host}:{port}  ({backend}, model {model}, effort {effort})"
        + (f", serving the built site from {dist}" if dist else ""))
    log("    GET  /api/cells             candidates, highest criteria score first")
    log("    GET  /api/cell/<cell_id>    the evidence record and any stored memos")
    log("    POST /api/chat              {cell_id, question, conversation_id?}; /api/chat/stream for NDJSON")
    log("    GET  /api/conversation/<id> a persisted transcript; /docs for the OpenAPI page")
    log("  local only, and the public-safe build does not offer the chat at all")
    uvicorn.run(app, host=host, port=port, log_level="warning")
