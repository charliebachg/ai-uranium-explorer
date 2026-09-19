"""The service the dashboard talks to: FastAPI over the store and the chat agent.

Same contract as the stdlib server it replaces — `/api/health`, `/api/cells`, `/api/cell/{id}`, `/api/chat`,
`/api/chat/stream` with the same NDJSON events — plus persisted conversations. Every endpoint reads the same
evidence record the map is drawn from, and no endpoint invents a number: a chat answer passes the same gate a
published memo does before it leaves this process.

The agent loop is synchronous and takes tens of seconds; streaming it means running it on a worker thread and
handing each event through a queue to the response generator, so the client sees tool calls as they happen.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from pathlib import Path
from typing import Any, Callable, Iterator

from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import StreamingResponse
from pydantic import BaseModel, Field

from ..prospect import serve as S
from ..store import connect
from ..prospect.chat import Conversation, ask
from . import persist

CELL_ID = r"^\d{4}_\d{4}$"
_CELL = re.compile(CELL_ID)
_END = object()


class ChatRequest(BaseModel):
    cell_id: str = Field(pattern=CELL_ID, description="looks like 0123_0045")
    question: str = Field(min_length=1, max_length=4000)
    conversation_id: str | None = None


class Health(BaseModel):
    ok: bool
    model: str
    effort: str
    backend: str


class Registry:
    """Live conversations for the life of the process; a known id that is not live is rehydrated from the store."""

    def __init__(self, db_path: Path | None) -> None:
        self.live: dict[str, Conversation] = {}
        self.lock = threading.Lock()
        self.db_path = db_path

    def get_or_create(self, cell_id: str, conversation_id: str | None, model: str, backend: str) -> Conversation:
        with self.lock:
            conv = self.live.get(conversation_id) if conversation_id else None
            if conv is not None and conv.cell_id == cell_id:
                return conv
            if conversation_id and conv is None:
                stored = persist.load_conversation(conversation_id, self.db_path)
                if stored and stored["cell_id"] == cell_id:
                    conv = Conversation(cell_id=cell_id, conversation_id=conversation_id)
                    conv.turns = [{"question": t["question"], "text": t["text"], "published": t["published"],
                                   "problems": t["problems"], "claims": t["claims"]} for t in stored["turns"]]
                    self.live[conversation_id] = conv
                    return conv
            conv = Conversation(cell_id=cell_id)
            self.live[conv.conversation_id] = conv
            persist.save_conversation(conv.conversation_id, cell_id, model, backend, self.db_path)
            return conv


def _cited_values(conv: Conversation, turn: dict[str, Any]) -> dict[str, Any]:
    ids = {v for c in (turn.get("claims") or []) for v in c.get("value_ids", [])}
    return {k: conv.values[k] for k in ids if k in conv.values}


def _done_payload(conv: Conversation, cell_id: str, turn: dict[str, Any]) -> dict[str, Any]:
    return {"conversation_id": conv.conversation_id, "cell_id": cell_id, "turn": turn,
            "values": _cited_values(conv, turn), "cost_usd": round(conv.cost_usd, 4)}


def create_app(backend_factory: Callable[[], Any], model: str, effort: str = "medium", backend_name: str = "openai",
               db_path: Path | None = None) -> FastAPI:
    app = FastAPI(title="AI Uranium Explorer service", version="0.2.0")
    app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])
    registry = Registry(db_path)
    app.state.registry = registry
    # a read-only connection never applies the schema, so the conversation tables are created here, once,
    # before the first read can ask for them
    connect(db_path).close()

    def run_turn(conv: Conversation, question: str, on_event: Callable[[dict[str, Any]], None]) -> dict[str, Any]:
        """One gated turn, persisted with the tool calls it made and the values it cited."""
        calls_before = len(conv.calls)
        t0 = time.monotonic()
        turn = ask(conv, question, backend_factory(), model=model, effort=effort, on_event=on_event)
        step = len(conv.turns)
        persist.save_turn(conv.conversation_id, step, turn, conv.calls[calls_before:], _cited_values(conv, turn),
                          time.monotonic() - t0, db_path)
        return turn

    @app.get("/api/health", response_model=Health)
    def health() -> Health:
        return Health(ok=True, model=model, effort=effort, backend=backend_name)

    @app.get("/api/cells")
    def cells(limit: int = Query(40, ge=1, le=200), model_key: str = Query("criteria", alias="model")) -> dict[str, Any]:
        return {"cells": S.candidates(limit, model_key)}

    @app.get("/api/cell/{cell_id}")
    def cell(cell_id: str) -> dict[str, Any]:
        if not _CELL.match(cell_id):
            raise HTTPException(400, "cell id looks like 0123_0045")
        return S.evidence(cell_id)

    @app.get("/api/cell/{cell_id}/conversations")
    def cell_conversations(cell_id: str) -> dict[str, Any]:
        if not _CELL.match(cell_id):
            raise HTTPException(400, "cell id looks like 0123_0045")
        return {"cell_id": cell_id, "conversations": persist.list_conversations(cell_id, db_path)}

    @app.get("/api/conversation/{conversation_id}")
    def conversation(conversation_id: str) -> dict[str, Any]:
        stored = persist.load_conversation(conversation_id, db_path)
        if stored is None:
            raise HTTPException(404, "no such conversation")
        return stored

    @app.post("/api/chat")
    def chat(body: ChatRequest) -> dict[str, Any]:
        conv = registry.get_or_create(body.cell_id, body.conversation_id, model, backend_name)
        try:
            turn = run_turn(conv, body.question.strip(), lambda _e: None)
        except Exception as err:  # the service says what broke rather than dying silently
            raise HTTPException(500, f"{type(err).__name__}: {err}") from err
        return _done_payload(conv, body.cell_id, turn)

    @app.post("/api/chat/stream")
    def chat_stream(body: ChatRequest) -> StreamingResponse:
        conv = registry.get_or_create(body.cell_id, body.conversation_id, model, backend_name)
        events: queue.Queue[Any] = queue.Queue()

        def work() -> None:
            try:
                turn = run_turn(conv, body.question.strip(), events.put)
                events.put({"type": "done", **_done_payload(conv, body.cell_id, turn)})
            except Exception as err:  # noqa: BLE001
                events.put({"type": "error", "error": f"{type(err).__name__}: {err}"})
            finally:
                events.put(_END)

        threading.Thread(target=work, name="lr-chat", daemon=True).start()

        def lines() -> Iterator[bytes]:
            while True:
                item = events.get()
                if item is _END:
                    return
                yield (json.dumps(item) + "\n").encode()

        return StreamingResponse(lines(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "Connection": "close"})

    return app
