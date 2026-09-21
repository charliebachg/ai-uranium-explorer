"""Session handles: opaque, expiring, bound to the caller's key, one run each.

A handle names an `analyst.session.Session`: that class is where B17, B18, B19 and B30 live, and this module
never re-states a rule it enforces. A dashboard session is unblinded; a scored session serves out-of-fold
scores, masks the cell's own label and blind-lists its files; a benchmark session does the same under a bench
id. The MCP server adds what the protocol needs around it: a handle the client carries on every call, an
expiry, the key it belongs to, a lock (one call at a time per session, because the session's store handle is
one connection), the abstentions and insights recorded through it, and a run directory with a manifest and
the spans of every call, so a session is a run in the runtime's sense and replays from what it wrote.

The manifest is the runtime's own (`runtime.manifest.Manifest`); the store hash it names is computed once per
process per store version rather than per session, because hashing a store the size of the live one on every
dashboard click would be a cost with no information in it.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

import duckdb

from . import TOOL_VERSION
from ..analyst import session as AS
from ..analyst.arms import Switches
from ..analyst.prompts import prompt_hashes as analyst_prompt_hashes
from ..backends.base import hash_text
from ..bench.pack import CELL_ID
from ..prospect import chat as CHAT
from ..prospect import memo
from ..runtime.manifest import Manifest
from ..runtime.runs import new_run_id
from ..store import connect
from ..store import snapshot as SN

#: how long a handle lives; fixed from the open, so `expires_at` means what it says
TTL_S = 4 * 3600
RUN_KIND = "mcp"
#: what `abstain` may be charged to
ABSTAIN_REASONS: tuple[str, ...] = ("not_measured", "outside_grid", "no_value", "out_of_scope")
#: an MCP session shows every part: a client that wants a part off does not call for it
ALL_ON = Switches(drillholes=True, label_context=True, oof_scores=True, effort_features=True, criteria=True)


class SessionError(Exception):
    """A handle the store would not honour, with the reason a client can act on."""

    def __init__(self, reason: str, rule: str = "session") -> None:
        super().__init__(reason)
        self.reason, self.rule = reason, rule


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


# ---------------------------------------------------------------- the store, read once per version


_PIN: dict[str, Any] = {}


def pinned_store() -> dict[str, Any]:
    """The store's hash and the snapshot it is, as `runtime.manifest.Manifest.start` records them, computed
    once per (path, mtime, size) for the life of the process."""
    db = SN.db_path()
    try:
        st = db.stat()
    except FileNotFoundError:
        return {"store_sha256": None, "snapshot": None}
    key = (str(db), st.st_mtime_ns, st.st_size)
    if _PIN.get("key") != key:
        try:
            _PIN["value"] = SN.pin(None, log=lambda *_a: None)
        except (OSError, RuntimeError):
            _PIN["value"] = {"store_sha256": None, "snapshot": None}
        _PIN["key"] = key
    return dict(_PIN["value"])


def cell_exists(cell_id: str) -> bool:
    con = connect(read_only=True)
    try:
        return con.execute("select 1 from derived.cell where cell_id = ?", [cell_id]).fetchone() is not None
    finally:
        con.close()


def single_fold(cell_id: str) -> int | None:
    """The one spatial fold the out-of-fold table holds for a cell, or None when it holds none or several."""
    con = connect(read_only=True)
    try:
        try:
            folds = {int(r[0]) for r in con.execute(
                "select distinct fold from derived.cell_score_oof where cell_id = ?", [cell_id]).fetchall()}
        except duckdb.Error:
            return None
    finally:
        con.close()
    return folds.pop() if len(folds) == 1 else None


def prompt_hashes() -> dict[str, str]:
    """Every fixed prompt text a client may be handed, hashed, so the manifest names what a role was told."""
    out = dict(analyst_prompt_hashes())
    out["memo_system"] = hash_text(memo.SYSTEM)
    out["chat_system"] = hash_text(CHAT.SYSTEM)
    return out


def claim_dir(runs_dir: Path, base: str) -> tuple[str, Path]:
    """A fresh run directory under `runs_dir`, the way `runtime.runs.claim_run_dir` claims one: the second
    session opened in the same second gets a -2 suffix."""
    for n in range(1, 1000):
        run_id = base if n == 1 else f"{base}-{n}"
        path = runs_dir / run_id
        try:
            path.mkdir(parents=True, exist_ok=False)
        except FileExistsError:
            continue
        return run_id, path
    raise RuntimeError(f"could not claim a run directory under {runs_dir / base}")


# ---------------------------------------------------------------- one live session


@dataclass
class LiveSession:
    session_id: str
    principal: str
    session: AS.Session
    run_id: str
    run_dir: Path
    manifest: Manifest
    opened_at: dt.datetime
    expires_at: dt.datetime
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)
    abstentions: list[dict[str, Any]] = field(default_factory=list)
    insights: list[dict[str, Any]] = field(default_factory=list)
    n_calls: int = 0
    closed: bool = False

    @property
    def shown(self) -> str:
        """The id the client knows the cell by: the bench id in a benchmark, the cell id otherwise."""
        return self.session.bench_id

    @property
    def blinded(self) -> bool:
        return self.session.blinded

    def expired(self, now: dt.datetime) -> bool:
        return now >= self.expires_at

    def register(self, values: Mapping[str, dict[str, Any]], expert: bool = False) -> None:
        """Values minted outside the session's own tools (a rank, an offset, an insight) join its registry, so
        `check_claims` binds to them like any other; expert ones are remembered for B19."""
        self.session.values |= dict(values)
        if expert:
            self.session.expert_ids |= set(values)

    def record(self, tool: str, payload: dict[str, Any], expert: bool = False) -> Path:
        """A result the session's own dispatcher did not produce (`hole_crosscheck`, an insight) joins the
        registry and the text the gate may find a quoted number in, and is staged under the run like a tool
        result, so the run replays from its files the way a loop's does."""
        self.register(payload.get("values") or {}, expert=expert)
        self.session.context += "\n" + memo.quotable(payload)
        stage = self.run_dir / "stage"
        stage.mkdir(parents=True, exist_ok=True)
        path = stage / f"mcp_{self.n_calls:03d}_{tool}.json"
        path.write_text(json.dumps(payload, indent=1, default=str))
        return path

    def write_manifest(self, final: bool = False) -> Path:
        m = self.manifest
        fields = self.session.manifest_fields()
        m.scores_seen = list(fields["scores_seen"])
        m.blind_list_sha256 = fields["blind_list_hash"]
        m.config.update({
            "session": {k: v for k, v in fields.items() if k not in ("scores_seen", "blind_list_hash")},
            "n_calls": self.n_calls, "expires_at": _iso(self.expires_at),
            "abstentions": list(self.abstentions), "insights": list(self.insights),
        })
        if final:
            m.config["value_ids"] = sorted(self.session.values)
            m.finish(0.0)
        return m.write(self.run_dir)

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        try:
            self.write_manifest(final=True)
        finally:
            self.session.close()


# ---------------------------------------------------------------- the store of handles


class SessionStore:
    """Every live handle, keyed by id; expired ones are closed on the next sweep."""

    def __init__(self, *, runs_dir: Path, tools: Mapping[str, Callable[..., Any]], ttl_s: float = TTL_S,
                 clock: Callable[[], dt.datetime] = now_utc, pin: Callable[[], dict[str, Any]] = pinned_store) -> None:
        self.runs_dir = runs_dir
        self.tools = tools
        self.ttl_s = float(ttl_s)
        self.clock = clock
        self.pin = pin
        self._live: dict[str, LiveSession] = {}
        self._lock = threading.Lock()

    def open(self, *, cell_id: str, purpose: str, fold: int | None, bench_id: str | None,
             principal: str) -> LiveSession:
        self.sweep()
        if purpose not in AS.PURPOSES:
            raise SessionError(f"purpose must be one of {AS.PURPOSES}, not {purpose!r}", "purpose")
        if not CELL_ID.fullmatch(str(cell_id)):
            raise SessionError(f"a cell id looks like 0123_0045, not {cell_id!r}", "outside_grid")
        if not cell_exists(cell_id):
            raise SessionError(f"cell {cell_id} is outside the grid", "outside_grid")
        blinded = purpose != "dashboard"
        if blinded and fold is None:
            fold = single_fold(cell_id)
        sid = uuid.uuid4().hex
        run_id, run_dir = claim_dir(self.runs_dir, new_run_id(RUN_KIND))
        shown = (bench_id or f"b-{sid[:6]}") if purpose == "benchmark" else None
        session = AS.Session.open(cell_id, purpose, fold=fold, switches=ALL_ON, bench_id=shown,
                                  stage=run_dir / "stage", tools=self.tools)
        opened = self.clock()
        pinned = self.pin()
        manifest = Manifest(
            run_id=run_id, kind="other", started_at=_iso(opened),
            config={"mcp": True, "tool_contract": TOOL_VERSION, "session_id": sid, "purpose": purpose,
                    "cell_id": cell_id, "shown_id": session.bench_id, "fold": fold, "principal": principal},
            models={}, prompt_hashes=prompt_hashes(), store_sha256=pinned.get("store_sha256"),
            snapshot=pinned.get("snapshot"), git_commit=SN._git_commit(),
            notes=f"MCP session {sid}: {purpose} purpose, opened by {principal}",
        )
        live = LiveSession(session_id=sid, principal=principal, session=session, run_id=run_id, run_dir=run_dir,
                           manifest=manifest, opened_at=opened,
                           expires_at=opened + dt.timedelta(seconds=self.ttl_s))
        live.write_manifest()
        with self._lock:
            self._live[sid] = live
        return live

    def get(self, session_id: Any, principal: str) -> LiveSession:
        """The live session a handle names, if it is this caller's and has not expired."""
        if not isinstance(session_id, str) or not session_id:
            raise SessionError("session_id is required: call open_session first and pass the handle it returned")
        with self._lock:
            live = self._live.get(session_id)
        if live is None:
            raise SessionError("no such session: it may have expired or belong to another server process; "
                               "open a new one")
        if live.principal != principal:
            raise SessionError("this session was opened with another key; open your own", "key")
        if live.expired(self.clock()):
            self._close(session_id)
            raise SessionError("this session has expired; open a new one", "expired")
        return live

    def _close(self, session_id: str) -> None:
        with self._lock:
            live = self._live.pop(session_id, None)
        if live is not None:
            live.close()

    def sweep(self) -> int:
        """Close every expired session; returns how many went."""
        now = self.clock()
        with self._lock:
            gone = [sid for sid, live in self._live.items() if live.expired(now)]
        for sid in gone:
            self._close(sid)
        return len(gone)

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._live)
        for sid in ids:
            self._close(sid)

    def __len__(self) -> int:
        return len(self._live)
