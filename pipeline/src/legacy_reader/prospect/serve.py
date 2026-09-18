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
import re
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable
from urllib.parse import parse_qs, urlparse

from ..store import connect
from . import tools as T
from .chat import Conversation, ask

DEFAULT_PORT = 8787
#: conversations live in memory for the life of the process; a demo does not need them to survive a restart
CONVERSATIONS: dict[str, Conversation] = {}
LOCK = threading.Lock()

CELL_ID = re.compile(r"^\d{4}_\d{4}$")


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


def make_handler(backend_factory: Callable[[], Any], model: str, effort: str) -> type:
    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quieter than the default
            pass

        def _send(self, status: int, payload: Any) -> None:
            body = json.dumps(payload).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            # the Vite dev server runs on another port; this service is localhost-only either way
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.end_headers()
            self.wfile.write(body)

        def do_OPTIONS(self) -> None:  # noqa: N802
            self._send(204, {})

        def _stream(self, conv: Conversation, question: str, cell_id: str) -> None:
            """Newline-delimited JSON, one line per step, flushed as it happens.

            The connection is closed at the end rather than length-delimited, because the length is not known
            until the answer is. The client reads to EOF.
            """
            self.send_response(200)
            self.send_header("Content-Type", "application/x-ndjson")
            self.send_header("Cache-Control", "no-store")
            self.send_header("Access-Control-Allow-Origin", "*")
            self.send_header("Connection", "close")
            self.end_headers()
            self.close_connection = True

            def emit(event: dict[str, Any]) -> None:
                try:
                    self.wfile.write((json.dumps(event) + "\n").encode())
                    self.wfile.flush()
                except (BrokenPipeError, ConnectionResetError):
                    pass  # the reader navigated away; the run finishes and is simply not delivered

            try:
                turn = ask(conv, question, backend_factory(), model=model, effort=effort, on_event=emit)
            except Exception as err:
                return emit({"type": "error", "error": f"{type(err).__name__}: {err}"})
            emit({
                "type": "done",
                "conversation_id": conv.conversation_id,
                "cell_id": cell_id,
                "turn": turn,
                "values": {k: conv.values[k] for k in
                           {v for c in (turn.get("claims") or []) for v in c.get("value_ids", [])}
                           if k in conv.values},
                "cost_usd": round(conv.cost_usd, 4),
            })

        def do_GET(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            query = parse_qs(url.query)
            try:
                if url.path == "/api/health":
                    return self._send(200, {"ok": True, "model": model, "effort": effort})
                if url.path == "/api/cells":
                    limit = int((query.get("limit") or ["40"])[0])
                    return self._send(200, {"cells": candidates(min(limit, 200))})
                if url.path.startswith("/api/cell/"):
                    cell_id = url.path.rsplit("/", 1)[-1]
                    if not CELL_ID.match(cell_id):
                        return self._send(400, {"error": "cell id looks like 0123_0045"})
                    return self._send(200, evidence(cell_id))
                return self._send(404, {"error": "no such endpoint"})
            except Exception as err:  # a demo service says what broke rather than dying silently
                return self._send(500, {"error": f"{type(err).__name__}: {err}"})

        def do_POST(self) -> None:  # noqa: N802
            url = urlparse(self.path)
            length = int(self.headers.get("Content-Length") or 0)
            try:
                body = json.loads(self.rfile.read(length) or b"{}")
            except json.JSONDecodeError:
                return self._send(400, {"error": "body must be JSON"})
            if url.path not in ("/api/chat", "/api/chat/stream"):
                return self._send(404, {"error": "no such endpoint"})

            cell_id = str(body.get("cell_id") or "")
            question = str(body.get("question") or "").strip()
            if not CELL_ID.match(cell_id):
                return self._send(400, {"error": "cell id looks like 0123_0045"})
            if not question:
                return self._send(400, {"error": "ask something"})

            key = str(body.get("conversation_id") or "") or None
            with LOCK:
                conv = CONVERSATIONS.get(key) if key else None
                if conv is None or conv.cell_id != cell_id:
                    conv = Conversation(cell_id=cell_id)
                    CONVERSATIONS[conv.conversation_id] = conv
            if url.path == "/api/chat/stream":
                return self._stream(conv, question, cell_id)

            try:
                turn = ask(conv, question, backend_factory(), model=model, effort=effort)
            except Exception as err:
                return self._send(500, {"error": f"{type(err).__name__}: {err}"})
            return self._send(200, {
                "conversation_id": conv.conversation_id,
                "cell_id": cell_id,
                "turn": turn,
                "values": {k: conv.values[k] for k in
                           {v for c in (turn.get("claims") or []) for v in c.get("value_ids", [])}
                           if k in conv.values},
                "cost_usd": round(conv.cost_usd, 4),
            })

    return Handler


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
          backend: str = "openai", log: Callable[[str], None] = print) -> None:
    chosen, model = make_backend(backend, model)
    handler = make_handler(lambda: chosen, model, effort)
    server = ThreadingHTTPServer(("127.0.0.1", port), handler)
    log(f"  listening on http://127.0.0.1:{port}  ({backend}, model {model}, effort {effort})")
    log("    GET  /api/cells             candidates, highest criteria score first")
    log("    GET  /api/cell/<cell_id>    the evidence record and any stored memos")
    log("    POST /api/chat              {cell_id, question, conversation_id?}")
    log("  local only, and the public-safe build does not offer the chat at all")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        log("  stopped")
    finally:
        server.server_close()
