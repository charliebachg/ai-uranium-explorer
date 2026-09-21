"""The service the dashboard talks to: FastAPI over the store and the chat agent.

Same contract as the stdlib server it replaces — `/api/health`, `/api/cells`, `/api/cell/{id}`, `/api/chat`,
`/api/chat/stream` with the same NDJSON events — plus persisted conversations, and the same tools over MCP at
`/mcp` (`uranium_explorer.mcp`). Every endpoint reads the same evidence record the map is drawn from,
and no endpoint invents a number: a chat answer passes the same gate a published memo does before it leaves
this process.

The agent loop is synchronous and takes tens of seconds; streaming it means running it on a worker thread and
handing each event through a queue to the response generator, so the client sees tool calls as they happen.

Who is calling comes from one key register shared with the MCP server (`api.auth`: roles over the MCP
scopes; a loopback client with no register is `local` with every role), and every row a request writes says
who asked. Anything longer than a request is a job (`api.jobs`): submitted, polled, cancelled over
`/api/jobs`, run on a bounded pool inside this process because it holds the store's one read-write handle.
"""

from __future__ import annotations

import json
import queue
import re
import threading
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, AsyncIterator, Callable, Iterator

from fastapi import Depends, FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, Field

from ..mcp import server as MCPS
from ..mcp import transport as MCPT
from ..store import connect
from ..prospect.chat import Conversation, ask
from . import auth as AUTH
from . import jobs as JOBS
from . import persist
from . import review as REVIEW
from .reads import Candidates, EvidenceCache
from .models import (CellConversations, CellJobs, Cells, ChatResponse, ConversationRecord, Evidence, Job,
                     JobRequest, Whoami)

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
    reads: str = "duckdb"                      # postgis | duckdb: where the candidate list came from last
    evidence_cache: dict[str, int] = Field(default_factory=dict)


class Registry:
    """Live conversations for the life of the process; a known id that is not live is rehydrated from the store."""

    def __init__(self, db_path: Path | None) -> None:
        self.live: dict[str, Conversation] = {}
        self.lock = threading.Lock()
        self.db_path = db_path

    def get_or_create(self, cell_id: str, conversation_id: str | None, model: str, backend: str,
                      requested_by: str | None = None) -> Conversation:
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
            persist.save_conversation(conv.conversation_id, cell_id, model, backend, self.db_path,
                                      requested_by=requested_by)
            return conv


def _cited_values(conv: Conversation, turn: dict[str, Any]) -> dict[str, Any]:
    ids = {v for c in (turn.get("claims") or []) for v in c.get("value_ids", [])}
    return {k: conv.values[k] for k in ids if k in conv.values}


def _done_payload(conv: Conversation, cell_id: str, turn: dict[str, Any]) -> dict[str, Any]:
    return {"conversation_id": conv.conversation_id, "cell_id": cell_id, "turn": turn,
            "values": _cited_values(conv, turn), "cost_usd": round(conv.cost_usd, 4)}


#: the Vite dev server, the only page allowed to call the API from another origin
DEV_ORIGINS = ["http://localhost:5173", "http://127.0.0.1:5173"]


def create_app(backend_factory: Callable[[], Any], model: str, effort: str = "medium", backend_name: str = "openai",
               db_path: Path | None = None, web_dist: Path | None = None, warm: bool = False,
               keyring: AUTH.Keyring | None = None, jobs: JOBS.Runner | None = None) -> FastAPI:
    app = FastAPI(title="AI Uranium Explorer service", version="0.3.0")
    # The built site is served from this origin, so it needs no CORS; the Vite dev server is the one
    # cross-origin caller. Anything else would let a page on any site drive a local instance that grants
    # every scope to local callers, so the list is closed.
    app.add_middleware(CORSMiddleware, allow_origins=DEV_ORIGINS, allow_methods=["*"], allow_headers=["*"])
    registry = Registry(db_path)
    app.state.registry = registry
    # one key register for the API and the MCP route (UE_MCP_KEYS); a test hands in its own
    keyring = keyring or AUTH.Keyring.from_env()
    app.state.keyring = keyring
    # a read-only connection never applies the schema, so the conversation tables are created here, once,
    # before the first read can ask for them
    connect(db_path).close()
    # the job runner: its recovery pass marks what an earlier process left running; the pool starts on the
    # first job. Installed as the module's default so `jobs.submit_analyst` inside this process lands here.
    runner = JOBS.install(jobs if jobs is not None else JOBS.Runner(db_path))
    app.state.jobs = runner
    evidence_cache = EvidenceCache()
    candidates = Candidates()
    app.state.evidence_cache, app.state.candidates = evidence_cache, candidates
    if warm:
        # the cells the rail lists first are the ones a reader clicks first
        try:
            evidence_cache.warm([c["cell_id"] for c in candidates.get(40, "criteria")], log=print)
        except Exception:  # noqa: BLE001 - no store yet is a state the health route reports, not a crash
            pass

    def run_turn(conv: Conversation, question: str, on_event: Callable[[dict[str, Any]], None],
                 requested_by: str | None = None) -> dict[str, Any]:
        """One gated turn, persisted with the tool calls it made, the values it cited and who asked."""
        calls_before = len(conv.calls)
        t0 = time.monotonic()
        if requested_by:
            conv.requested_by = requested_by   # the author an insight is recorded under, never the key
        turn = ask(conv, question, backend_factory(), model=model, effort=effort, on_event=on_event)
        step = len(conv.turns)
        persist.save_turn(conv.conversation_id, step, turn, conv.calls[calls_before:], _cited_values(conv, turn),
                          time.monotonic() - t0, db_path, requested_by=requested_by)
        return turn

    @app.get("/api/health", response_model=Health)
    def health() -> Health:
        return Health(ok=True, model=model, effort=effort, backend=backend_name, reads=candidates.source,
                      evidence_cache={"hits": evidence_cache.hits, "misses": evidence_cache.misses})

    @app.get("/api/whoami", response_model=Whoami)
    def whoami(who: AUTH.Principal = Depends(AUTH.current)) -> dict[str, Any]:
        """The principal the request's key resolves to: how a page checks a key before it relies on it."""
        return {"name": who.name, "scopes": sorted(who.scopes), "roles": AUTH.roles_of(who)}

    @app.get("/api/cells", response_model=Cells)
    def cells(limit: int = Query(40, ge=1, le=200), model_key: str = Query("criteria", alias="model")) -> dict[str, Any]:
        return {"cells": candidates.get(limit, model_key)}

    @app.get("/api/cell/{cell_id}", response_model=Evidence)
    def cell(cell_id: str) -> dict[str, Any]:
        if not _CELL.match(cell_id):
            raise HTTPException(400, "cell id looks like 0123_0045")
        return evidence_cache.get(cell_id)

    @app.get("/api/cell/{cell_id}/conversations", response_model=CellConversations)
    def cell_conversations(cell_id: str) -> dict[str, Any]:
        if not _CELL.match(cell_id):
            raise HTTPException(400, "cell id looks like 0123_0045")
        return {"cell_id": cell_id, "conversations": persist.list_conversations(cell_id, db_path)}

    @app.get("/api/conversation/{conversation_id}", response_model=ConversationRecord)
    def conversation(conversation_id: str) -> dict[str, Any]:
        stored = persist.load_conversation(conversation_id, db_path)
        if stored is None:
            raise HTTPException(404, "no such conversation")
        return stored

    @app.post("/api/chat", response_model=ChatResponse)
    def chat(body: ChatRequest, who: AUTH.Principal = Depends(AUTH.require("geologist"))) -> dict[str, Any]:
        conv = registry.get_or_create(body.cell_id, body.conversation_id, model, backend_name, requested_by=who.name)
        try:
            turn = run_turn(conv, body.question.strip(), lambda _e: None, requested_by=who.name)
        except Exception as err:  # the service says what broke rather than dying silently
            raise HTTPException(500, f"{type(err).__name__}: {err}") from err
        return _done_payload(conv, body.cell_id, turn)

    @app.post("/api/chat/stream")
    def chat_stream(body: ChatRequest, who: AUTH.Principal = Depends(AUTH.require("geologist"))) -> StreamingResponse:
        conv = registry.get_or_create(body.cell_id, body.conversation_id, model, backend_name, requested_by=who.name)
        events: queue.Queue[Any] = queue.Queue()

        def work() -> None:
            try:
                turn = run_turn(conv, body.question.strip(), events.put, requested_by=who.name)
                events.put({"type": "done", **_done_payload(conv, body.cell_id, turn)})
            except Exception as err:  # noqa: BLE001
                events.put({"type": "error", "error": f"{type(err).__name__}: {err}"})
            finally:
                events.put(_END)

        threading.Thread(target=work, name="ue-chat", daemon=True).start()

        def lines() -> Iterator[bytes]:
            while True:
                item = events.get()
                if item is _END:
                    return
                yield (json.dumps(item) + "\n").encode()

        return StreamingResponse(lines(), media_type="application/x-ndjson",
                                 headers={"Cache-Control": "no-store", "Connection": "close"})

    # ---------------------------------------------------------------- jobs (anything over a second)

    def kind_of(name: str) -> JOBS.JobKind:
        spec = runner.kinds.get(name)
        if spec is None:
            raise HTTPException(404, f"no job kind named {name!r}; the kinds are {sorted(runner.kinds)}")
        return spec

    @app.post("/api/jobs", response_model=Job, status_code=202)
    def submit_job(body: JobRequest, who: AUTH.Principal = Depends(AUTH.current)) -> dict[str, Any]:
        """Queue a job of a registered kind; the row comes back at once and is polled at /api/jobs/{id}. The
        role a kind needs is the kind's own (analyst: admin); the reason for a refusal is the response."""
        spec = kind_of(body.kind)
        AUTH.check(who, spec.role, f"a {body.kind} job")
        try:
            job_id = runner.submit(body.kind, body.cell_id, body.args, requested_by=who.name)
        except JOBS.JobRefused as err:
            raise HTTPException(400, str(err)) from err
        return runner.get(job_id)

    @app.get("/api/jobs/{job_id}", response_model=Job)
    def job(job_id: str, _who: AUTH.Principal = Depends(AUTH.require("viewer"))) -> dict[str, Any]:
        try:
            return runner.get(job_id)
        except JOBS.JobNotFound as err:
            raise HTTPException(404, str(err)) from err

    @app.get("/api/cell/{cell_id}/jobs", response_model=CellJobs)
    def cell_jobs(cell_id: str, _who: AUTH.Principal = Depends(AUTH.require("viewer"))) -> dict[str, Any]:
        if not _CELL.match(cell_id):
            raise HTTPException(400, "cell id looks like 0123_0045")
        return {"cell_id": cell_id, "jobs": runner.for_cell(cell_id)}

    @app.post("/api/jobs/{job_id}/cancel", response_model=Job)
    def cancel_job(job_id: str, who: AUTH.Principal = Depends(AUTH.current)) -> dict[str, Any]:
        """Stop a job: queued, it never starts; running, it stops at its next model call. Needs the role the
        job's kind needs. A finished job is returned as it is."""
        try:
            row = runner.get(job_id)
        except JOBS.JobNotFound as err:
            raise HTTPException(404, str(err)) from err
        AUTH.check(who, kind_of(row["kind"]).role, f"cancelling a {row['kind']} job")
        return runner.cancel(job_id)

    app.include_router(REVIEW.build_router(db_path))   # the extractor's review queue: api/review.py

    # the same tools over MCP, in this process and on this store connection mode: one route, and
    # the transport's session manager running inside the app's lifespan; the same keyring, and the job runner
    # behind `run_analyst`. Declared before the site's catch-all.
    MCPT.mount(app, MCPS.build(keyring=keyring, jobs=runner), MCPT.MCP_PATH)
    previous_lifespan = app.router.lifespan_context

    @asynccontextmanager
    async def lifespan(a: Any) -> AsyncIterator[Any]:
        async with previous_lifespan(a) as state:
            try:
                yield state
            finally:
                runner.close()   # no new jobs; what is running finishes, what is queued is marked on the next start

    app.router.lifespan_context = lifespan

    if web_dist is not None and (web_dist / "index.html").is_file():
        # the built site, from the same process: assets and data as files, every other path the app's own
        # router handles from index.html. Declared after the API routes so /api/* is never shadowed.
        for sub in ("assets", "data", "vendor", "tiles"):
            if (web_dist / sub).is_dir():
                app.mount(f"/{sub}", StaticFiles(directory=web_dist / sub), name=sub)

        @app.get("/{path:path}", include_in_schema=False)
        def site(path: str) -> FileResponse:
            candidate = (web_dist / path) if path else None
            if candidate and candidate.is_file() and web_dist in candidate.resolve().parents:
                return FileResponse(candidate)
            return FileResponse(web_dist / "index.html")

    return app
