"""Background jobs: anything over a second runs here, in this process, with a durable row per job.

Why in-process rather than a queue beside the API: DuckDB allows one read-write connection per file and the
serving process holds it (`store.one_mode`), so a worker in another process could not publish a chain into
the store the API is reading. The pool is a bounded set of threads inside the API; a job's state lives in
`agent.job` and is rewritten as it changes, so a restart shows every job's last state, and a job that was
running when the process died is marked failed with that reason rather than left running for ever.

A job kind is a function plus the role a submitter needs, registered in one table (`KINDS`). The first kind is
`analyst`: the staged loop over one **enabled** cell (any other cell is refused with that
reason), through the same `run_cells` that `ue arm chain` runs, so the chain lands in `agent.chain*` exactly
as an offline one does and the evidence panel lists it. A job has a budget in dollars; the process has a
budget across jobs (`UE_JOB_SESSION_BUDGET_USD`), and a submission that would take the total past it is
refused before anything is queued. Progress is the stage names as the loop's spans close, read from the run's
`spans.jsonl` while the run is on, so the row says where the chain is without the loop knowing about jobs.

Two names are the hook for the interface agent and importable without the app: `submit_analyst(cell_id, *,
reason, expert_ids, budget_usd, requested_by)` returns a job id and `get(job_id)` its row. They go through
the runner the app installed, or one made lazily on the default store when no app is running.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import threading
import tomllib
import uuid
from concurrent.futures import Future, ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Iterator

from ..paths import PATHS
from ..store import connect

STATUSES: tuple[str, ...] = ("queued", "running", "done", "failed", "cancelled")
FINAL: frozenset[str] = frozenset({"done", "failed", "cancelled"})
#: a cell id, as every route spells it
CELL = re.compile(r"^\d{4}_\d{4}$")
#: what a job of the analyst kind may spend, by default and at most
DEFAULT_BUDGET_USD = 0.50
MAX_BUDGET_USD = 2.00
#: the process-wide ceiling across jobs; read by name, never printed
SESSION_BUDGET_VAR = "UE_JOB_SESSION_BUDGET_USD"
DEFAULT_SESSION_BUDGET_USD = 5.0
WORKERS_VAR = "UE_JOB_WORKERS"
DEFAULT_WORKERS = 2
#: the analyst arm a job runs unless asked otherwise: pay-per-token through the router (`--backend auto`)
DEFAULT_ARM = "v1-openrouter"
DEFAULT_BACKEND = "auto"
#: the reason a job carries after the process it ran in went away
RESTARTED = "process restarted"

COLUMNS: tuple[str, ...] = ("job_id", "kind", "cell_id", "status", "requested_by", "args_json", "created_at",
                            "started_at", "finished_at", "progress_json", "result_json", "error", "run_id")


class JobRefused(Exception):
    """A submission the runner will not take, with a reason a caller can act on (a 4xx, never a crash)."""


class JobNotFound(KeyError):
    """No job with that id in the store."""

    def __str__(self) -> str:  # a KeyError quotes its argument; the reason reads better plain
        return str(self.args[0]) if self.args else "no such job"


class JobCancelled(Exception):
    """Raised inside a job when a cancel was asked for and the job reached a point it could stop at."""


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.UTC)


def _iso(t: dt.datetime) -> str:
    return t.isoformat(timespec="seconds")


def enabled_cells(path: Path | None = None) -> set[str]:
    """The enabled cells, read from the same file `ue arm chain --enabled` reads."""
    doc = tomllib.loads((path or PATHS.pipeline / "knowledge" / "enabled_cells.toml").read_text())
    return {str(c["id"]) for c in doc.get("cell", [])}


# ---------------------------------------------------------------- what a running job is handed


class Progress:
    """The job's side of the runner: record an event, learn the run id, and find out whether a cancel was
    asked for. Events are appended to the row as they happen, so a poll sees them before the job ends."""

    def __init__(self, runner: "Runner", job_id: str, cancel: threading.Event) -> None:
        self.runner, self.job_id, self.cancel = runner, job_id, cancel

    def event(self, name: str, **detail: Any) -> None:
        self.runner._append_progress(self.job_id, {"at": _iso(self.runner.clock()), "event": name, **detail})

    def log(self, line: str) -> None:
        """A harness log line, kept as an event: `run_cells` says what it preloaded and what each cell got."""
        text = line.strip()
        if text:
            self.event("log", text=text)

    def run_claimed(self, run_id: str) -> None:
        self.runner._set_run(self.job_id, run_id)
        self.event("run", run_id=run_id)

    @property
    def cancelled(self) -> bool:
        return self.cancel.is_set()

    def check(self) -> None:
        if self.cancel.is_set():
            raise JobCancelled("cancelled")


@dataclass(frozen=True)
class JobKind:
    """A kind of job: its name, the role a submitter needs, how it checks and normalises its arguments before
    anything is queued (raising `JobRefused` with the reason), what it will spend, and the work itself."""

    name: str
    role: str
    run: Callable[[dict[str, Any], Progress], dict[str, Any]]
    prepare: Callable[[str | None, dict[str, Any]], dict[str, Any]] = lambda cell_id, args: dict(args)
    budget: Callable[[dict[str, Any]], float] = lambda args: 0.0


#: the registered kinds, by name; `analyst` joins at the bottom of this module
KINDS: dict[str, JobKind] = {}


def register(kind: JobKind) -> JobKind:
    KINDS[kind.name] = kind
    return kind


# ---------------------------------------------------------------- the runner


@dataclass
class _Live:
    cancel: threading.Event = field(default_factory=threading.Event)
    future: Future[Any] | None = None
    reserved_usd: float = 0.0


class Runner:
    """A bounded thread pool over durable job rows in `agent.job` at `db_path` (the default store when None).

    The pool starts on the first submission, so building a runner costs one store open: the recovery pass
    that marks jobs left queued or running by an earlier process as failed with `process restarted`. Every
    row operation opens its own short connection, as `persist` does, and one at a time: outside the serving
    process's one mode each open re-applies the schema, and two threads doing that at once conflict in the
    catalog. Holding one connection open instead would refuse every read-only open beside it."""

    def __init__(self, db_path: Path | None = None, *, kinds: dict[str, JobKind] | None = None,
                 workers: int | None = None, session_budget_usd: float | None = None,
                 clock: Callable[[], dt.datetime] = now_utc) -> None:
        self.db_path = db_path
        self.kinds: dict[str, JobKind] = dict(kinds) if kinds is not None else KINDS
        self.workers = int(workers if workers is not None else os.environ.get(WORKERS_VAR, DEFAULT_WORKERS))
        self.session_budget_usd = float(session_budget_usd if session_budget_usd is not None
                                        else os.environ.get(SESSION_BUDGET_VAR, DEFAULT_SESSION_BUDGET_USD))
        self.clock = clock
        self._pool: ThreadPoolExecutor | None = None
        self._live: dict[str, _Live] = {}
        self._lock = threading.Lock()      # the in-memory state, and every row change (a read-modify-write)
        self._db_lock = threading.Lock()   # one store connection at a time; taken after _lock, never before
        self.spent_usd = 0.0       # what finished jobs of this process actually cost
        self.recovered = self.recover()

    # ---------------------------------------------------------------- rows

    @contextmanager
    def _db(self) -> Iterator[Any]:
        with self._db_lock:
            con = connect(self.db_path)
            try:
                yield con
            finally:
                con.close()

    @staticmethod
    def _decode(row: tuple[Any, ...]) -> dict[str, Any]:
        d = dict(zip(COLUMNS, row, strict=True))
        d["args"] = json.loads(d.pop("args_json") or "{}")
        d["progress"] = json.loads(d.pop("progress_json") or "[]")
        result = d.pop("result_json")
        d["result"] = json.loads(result) if result else None
        return d

    def _select(self, where: str, args: list[Any]) -> list[dict[str, Any]]:
        with self._db() as con:
            rows = con.execute(f"select {', '.join(COLUMNS)} from agent.job where {where}", args).fetchall()
        return [self._decode(r) for r in rows]

    def _update(self, job_id: str, **fields: Any) -> None:
        cols = list(fields)
        with self._db() as con:
            con.execute(f"update agent.job set {', '.join(f'{c} = ?' for c in cols)} where job_id = ?",
                        [fields[c] for c in cols] + [job_id])

    def _append_progress(self, job_id: str, event: dict[str, Any]) -> None:
        with self._lock:
            self._progress_locked(self.get(job_id), event)

    def _progress_locked(self, row: dict[str, Any], event: dict[str, Any], **fields: Any) -> None:
        """One event appended and any other fields set, by a caller holding the lock: the read-modify-write
        of the progress list is why every row change is serialised."""
        self._update(row["job_id"], progress_json=json.dumps([*row["progress"], event]), **fields)

    def _set_run(self, job_id: str, run_id: str) -> None:
        self._update(job_id, run_id=run_id)

    def recover(self) -> int:
        """Mark what an earlier process left behind: a running job failed with `process restarted`, a queued
        one likewise, because the pool that held it is gone. Returns how many rows changed."""
        at = _iso(self.clock())
        with self._db() as con:
            n = con.execute("select count(*) from agent.job where status in ('queued', 'running')").fetchone()[0]
            con.execute("update agent.job set status = 'failed', error = ?, finished_at = ? where status = 'running'",
                        [RESTARTED, at])
            con.execute("update agent.job set status = 'failed', error = ?, finished_at = ? where status = 'queued'",
                        [f"{RESTARTED} before the job ran", at])
        return int(n)

    # ---------------------------------------------------------------- reads

    def get(self, job_id: str) -> dict[str, Any]:
        rows = self._select("job_id = ?", [job_id])
        if not rows:
            raise JobNotFound(f"no job {job_id!r}")
        return rows[0]

    def for_cell(self, cell_id: str, limit: int = 50) -> list[dict[str, Any]]:
        return self._select("cell_id = ? order by created_at desc, job_id desc limit ?", [cell_id, limit])

    # ---------------------------------------------------------------- submit, cancel

    def _pool_started(self) -> ThreadPoolExecutor:
        with self._lock:
            if self._pool is None:
                self._pool = ThreadPoolExecutor(max_workers=max(1, self.workers), thread_name_prefix="ue-job")
            return self._pool

    def _committed_locked(self) -> float:
        return self.spent_usd + sum(live.reserved_usd for live in self._live.values())

    def committed_usd(self) -> float:
        """What this process has spent on finished jobs plus what its queued and running jobs may still spend."""
        with self._lock:
            return self._committed_locked()

    def submit(self, kind: str, cell_id: str | None, args: dict[str, Any] | None, requested_by: str) -> str:
        spec = self.kinds.get(kind)
        if spec is None:
            raise JobRefused(f"no job kind named {kind!r}; the kinds are {sorted(self.kinds)}")
        if cell_id is not None and not CELL.match(cell_id):
            raise JobRefused(f"a cell id looks like 0123_0045, not {cell_id!r}")
        prepared = spec.prepare(cell_id, dict(args or {}))
        wanted = float(spec.budget(prepared))
        job_id = uuid.uuid4().hex[:16]
        at = _iso(self.clock())
        queued = {"at": at, "event": "queued"}
        live = _Live(reserved_usd=wanted)
        with self._lock:
            # the budget is checked and the reservation made under one lock, so two submissions cannot both
            # fit into the same remainder
            committed = self._committed_locked()
            if wanted > 0 and committed + wanted > self.session_budget_usd:
                raise JobRefused(f"the session budget of ${self.session_budget_usd:.2f} ({SESSION_BUDGET_VAR}) leaves "
                                 f"${max(0.0, self.session_budget_usd - committed):.2f}; this job asks for ${wanted:.2f}")
            with self._db() as con:
                con.execute(
                    f"insert into agent.job ({', '.join(COLUMNS)}) values ({', '.join('?' * len(COLUMNS))})",
                    [job_id, kind, cell_id, "queued", requested_by, json.dumps(prepared), at, None, None,
                     json.dumps([queued]), None, None, None])
            self._live[job_id] = live
        live.future = self._pool_started().submit(self._work, job_id, spec, live)
        return job_id

    def cancel(self, job_id: str) -> dict[str, Any]:
        """Ask a job to stop. Queued: it never starts. Running: the flag is set and the job stops at the next
        point it checks (the analyst: its next model call); the row stays running until then. Finished: the
        row as it is, unchanged. Decided under the lock, so a job cannot start between the look and the word."""
        with self._lock:
            row = self.get(job_id)
            live = self._live.get(job_id)
            if row["status"] in FINAL or live is None:
                return row
            live.cancel.set()
            if row["status"] == "queued":
                self._finish_locked(row, "cancelled", error="cancelled before it started")
            else:
                self._progress_locked(row, {"at": _iso(self.clock()), "event": "cancel_requested"})
        return self.get(job_id)

    # ---------------------------------------------------------------- the worker

    def _finish_locked(self, row: dict[str, Any], status: str, *, result: dict[str, Any] | None = None,
                       error: str | None = None) -> None:
        if row["status"] in FINAL:
            return  # cancelled while queued, then the worker reached it: the first word stands
        at = _iso(self.clock())
        self._progress_locked(row, {"at": at, "event": status}, status=status, finished_at=at, error=error,
                              result_json=json.dumps(result) if result is not None else None)
        self._live.pop(row["job_id"], None)
        if result is not None:
            self.spent_usd += float(result.get("cost_usd") or 0.0)

    def _finish(self, job_id: str, status: str, *, result: dict[str, Any] | None = None,
                error: str | None = None) -> None:
        with self._lock:
            self._finish_locked(self.get(job_id), status, result=result, error=error)

    def _work(self, job_id: str, spec: JobKind, live: _Live) -> None:
        with self._lock:
            row = self.get(job_id)
            if row["status"] != "queued":
                return  # cancelled before it started: the row says so already
            at = _iso(self.clock())
            self._progress_locked(row, {"at": at, "event": "started"}, status="running", started_at=at)
            row = self.get(job_id)
        progress = Progress(self, job_id, live.cancel)
        try:
            result = spec.run(row, progress)
        except JobCancelled as stop:
            self._finish(job_id, "cancelled", error=str(stop) or "cancelled")
        except Exception as err:  # noqa: BLE001 - the row is the report; a job never takes the process down
            self._finish(job_id, "failed", error=f"{type(err).__name__}: {err}"[:2000])
        else:
            self._finish(job_id, "done", result=result)

    def wait(self, job_id: str, timeout: float = 60.0) -> dict[str, Any]:
        """Block until the job is final (a test's convenience; the API polls)."""
        with self._lock:
            live = self._live.get(job_id)
        if live is not None and live.future is not None:
            live.future.result(timeout=timeout)
        return self.get(job_id)

    def close(self) -> None:
        """Stop taking work; what is running finishes on its own thread, what is queued is left for the next
        process's recovery pass to mark."""
        with self._lock:
            pool, self._pool = self._pool, None
        if pool is not None:
            pool.shutdown(wait=False, cancel_futures=True)


# ---------------------------------------------------------------- the analyst kind


class _Cancellable:
    """The arm's backend with a cancel flag in front of every live call: a cancelled job stops at its next
    model call by raising the budget signal the harness already handles cleanly (the run stops, its manifest
    is finished, the cell is left pending), and everything else about the backend passes through."""

    def __init__(self, inner: Any, cancel: threading.Event) -> None:
        self._inner, self._cancel = inner, cancel

    def __getattr__(self, name: str) -> Any:
        return getattr(self._inner, name)

    def call(self, req: Any, on_delta: Callable[[str], None] | None = None) -> Any:
        if self._cancel.is_set():
            from ..runtime.spend import BudgetExhausted

            raise BudgetExhausted("the job was cancelled before this call")
        if on_delta is not None:
            from ..backends.cache import streams

            if streams(self._inner):
                return self._inner.call(req, on_delta=on_delta)
        return self._inner.call(req)


class SpanWatcher:
    """The stage names of a chain run, as events, while it runs: the loop writes a span per stage to the run's
    `spans.jsonl` as each closes (`runtime.tracing`), and this tails that file so the job row says which stage
    just finished without the loop knowing about jobs. Gate spans (one per attempt) are left out: a stage is
    the unit a reader waits for."""

    STAGE = "stage:"
    SKIP = frozenset({"stage:gate"})

    def __init__(self, progress: Progress, interval_s: float = 0.25) -> None:
        self.progress, self.interval_s = progress, interval_s
        self.path: Path | None = None
        self._offset = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self, path: Path) -> None:
        if self._thread is not None:
            return
        self.path = path
        self._thread = threading.Thread(target=self._run, name="ue-job-spans", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        while not self._stop.wait(self.interval_s):
            self.drain()

    def drain(self) -> int:
        """Read what closed since the last look; a partial last line waits for its newline."""
        if self.path is None or not self.path.is_file():
            return 0
        with self.path.open("rb") as fh:
            fh.seek(self._offset)
            chunk = fh.read()
        if not chunk:
            return 0
        end = chunk.rfind(b"\n")
        if end < 0:
            return 0
        self._offset += end + 1
        n = 0
        for line in chunk[:end].split(b"\n"):
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            name = str(rec.get("name") or "")
            if not name.startswith(self.STAGE) or name in self.SKIP:
                continue
            attrs = rec.get("attrs") or {}
            detail = {k: attrs[k] for k in ("round", "n_segments", "published", "decider") if k in attrs}
            self.progress.event(name, ms=rec.get("duration_ms"), **detail)
            n += 1
        return n

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
        self.drain()


@dataclass
class AnalystKind:
    """The staged analyst over one enabled cell, as `ue arm chain --enabled` runs it, as a job. Every piece a
    test needs to replace is a field: the backend factory (the fake loop backend), the session factory (a
    session over a fake tool world), the card renderer, the enabled set and the store."""

    arm: str = DEFAULT_ARM
    backend: str = DEFAULT_BACKEND
    db_path: Path | None = None
    enabled: Callable[[], set[str]] = enabled_cells
    backend_factory: Callable[[Any], Any] | None = None
    session_factory: Callable[..., Any] | None = None
    cards: Callable[..., dict[str, Path]] | None = None
    track: bool = True
    cache_root: Path | None = None

    def prepare(self, cell_id: str | None, args: dict[str, Any]) -> dict[str, Any]:
        from ..analyst.arms import load_arm

        if not cell_id:
            raise JobRefused("an analyst job is about one cell: pass cell_id")
        if cell_id not in self.enabled():
            raise JobRefused(f"the analyst runs only on the enabled cells (the dashboard's scope rule), and {cell_id} is not one of "
                             "them: it shows its scores and its evidence, and no analyst reading exists for it")
        try:
            budget = float(args.get("budget_usd", DEFAULT_BUDGET_USD))
        except (TypeError, ValueError) as err:
            raise JobRefused(f"budget_usd must be a number of dollars, not {args.get('budget_usd')!r}") from err
        if not 0 < budget <= MAX_BUDGET_USD:
            raise JobRefused(f"budget_usd must be over 0 and at most {MAX_BUDGET_USD:.2f}, not {budget:.2f}")
        arm_name = str(args.get("arm") or self.arm)
        try:
            arm = load_arm(arm_name)
        except (FileNotFoundError, ValueError) as err:
            raise JobRefused(f"no usable arm {arm_name!r}: {err}") from err
        if arm.agent != "v1":
            raise JobRefused(f"arm {arm_name!r} is not a staged (v1) arm; only the staged loop publishes a chain")
        if budget < arm.max_budget_usd_per_call:
            # the cached backend refuses a live call that could cross the run's budget by the arm's per-call
            # ceiling, so a budget under that ceiling would make no call at all and fail on its first stage
            raise JobRefused(f"budget_usd {budget:.2f} is under arm {arm_name!r}'s per-call ceiling of "
                             f"${arm.max_budget_usd_per_call:.2f}, so no model call could be made; ask for at "
                             f"least that (at most {MAX_BUDGET_USD:.2f})")
        expert_ids = args.get("expert_ids") or []
        if not isinstance(expert_ids, list) or not all(isinstance(x, str) for x in expert_ids):
            raise JobRefused("expert_ids must be a list of expert-tier ids")
        return {"arm": arm_name, "backend": self.backend, "budget_usd": round(budget, 4),
                "reason": str(args.get("reason") or ""), "expert_ids": list(expert_ids)}

    @staticmethod
    def budget(args: dict[str, Any]) -> float:
        return float(args["budget_usd"])

    def _backend_factory(self) -> Callable[[Any], Any]:
        if self.backend_factory is not None:
            return self.backend_factory
        from ..analyst.cli import _factory

        return _factory(self.backend)

    def run(self, job: dict[str, Any], progress: Progress) -> dict[str, Any]:
        from ..analyst import run as RUN
        from ..analyst import score as SC
        from ..analyst.arms import load_arm

        args, cell_id = job["args"], str(job["cell_id"])
        arm = load_arm(str(args["arm"]))
        inner = self._backend_factory()
        watcher = SpanWatcher(progress)
        open_session = self.session_factory or RUN.open_dashboard_session

        def session_factory(cid: str, arm_cfg: Any, stage: Path, shared: dict[str, Any]) -> Any:
            # the run has claimed its directory by now: <run dir>/stages/<cell>; from here the stages are spans
            rd = stage.parent.parent
            progress.run_claimed(rd.name)
            watcher.start(rd / "spans.jsonl")
            return open_session(cid, arm_cfg, stage, shared)

        con = connect(self.db_path)
        try:
            summary = RUN.run_cells(
                [cell_id], arm, lambda a: _Cancellable(inner(a), progress.cancel), float(args["budget_usd"]),
                log=progress.log, workers=1, track=self.track, cache_root=self.cache_root,
                session_factory=session_factory, cards=self.cards or RUN.render_cards, con=con)
        finally:
            con.close()
            watcher.stop()
        if progress.cancelled and cell_id in summary["pending"]:
            raise JobCancelled(f"cancelled at the next model call; run {summary['run_id']} is closed with the "
                               "cell pending")
        row = SC.read_cells(Path(summary["run_dir"])).get(cell_id)
        if row is None:
            why = ("its usage limit" if summary.get("usage_limited") else f"its budget of ${float(args['budget_usd']):.2f}")
            raise RuntimeError(f"the run stopped on {why} before the cell finished (run {summary['run_id']})")
        if row.get("error"):
            raise RuntimeError(f"the cell failed: {row['error']} (run {summary['run_id']})")
        answer = row.get("answer") or {}
        return {
            "chain_id": f"{summary['run_id']}:{cell_id}", "run_id": summary["run_id"], "arm": arm.name,
            "verdict": answer.get("verdict"), "probability": answer.get("probability"),
            "published": bool(row.get("published")), "problems": list(row.get("problems") or []),
            "cost_usd": round(float(row.get("cost_usd") or 0.0), 4), "spent_usd": summary["spent_usd"],
            "reason": args.get("reason", ""), "expert_ids": list(args.get("expert_ids") or []),
        }


def analyst_kind(**over: Any) -> JobKind:
    """The analyst kind over an `AnalystKind` with the given fields replaced (a test's fakes, or nothing)."""
    impl = AnalystKind(**over)
    return JobKind("analyst", "admin", impl.run, impl.prepare, impl.budget)


register(analyst_kind())


# ---------------------------------------------------------------- the hook for the interface agent

_default: Runner | None = None
_default_lock = threading.Lock()


def install(runner: Runner) -> Runner:
    """The app's runner becomes the one the module-level hook goes through, so a job the interface agent
    submits inside the API lands on the same pool as one submitted over HTTP."""
    global _default
    with _default_lock:
        _default = runner
    return runner


def runner() -> Runner:
    """The installed runner, or one made lazily on the default store when no app has installed one."""
    global _default
    with _default_lock:
        if _default is None:
            _default = Runner()
        return _default


def submit_analyst(cell_id: str, *, reason: str, expert_ids: list[str] | None = None,
                   budget_usd: float | None = None, requested_by: str = "local") -> str:
    """Queue the staged analyst over one enabled cell; returns the job id. `JobRefused` names the reason when
    the cell is not enabled, the budget is out of range or the session budget is spent."""
    args: dict[str, Any] = {"reason": reason, "expert_ids": list(expert_ids or [])}
    if budget_usd is not None:
        args["budget_usd"] = budget_usd
    return runner().submit("analyst", cell_id, args, requested_by=requested_by)


def get(job_id: str) -> dict[str, Any]:
    """The job's row: status, progress, result and error as `agent.job` holds them. `JobNotFound` otherwise."""
    return runner().get(job_id)


__all__ = ["CELL", "COLUMNS", "DEFAULT_ARM", "DEFAULT_BACKEND", "DEFAULT_BUDGET_USD", "DEFAULT_SESSION_BUDGET_USD",
           "FINAL", "KINDS", "MAX_BUDGET_USD", "RESTARTED", "SESSION_BUDGET_VAR", "STATUSES", "WORKERS_VAR",
           "AnalystKind", "JobCancelled", "JobKind", "JobNotFound", "JobRefused", "Progress", "Runner",
           "SpanWatcher", "analyst_kind", "enabled_cells", "get", "install", "register", "runner", "submit_analyst"]
