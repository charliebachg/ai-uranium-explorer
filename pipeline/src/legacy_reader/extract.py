"""`lr extract`: the scheduler. One routed page per `claude -p` call, paced, capped, cached, resumable.

Design constraints that show up in the code:
- **The subscription is shared.** At most `--max-calls` calls per invocation (default 120), 2 workers,
  at least 4 s between launches, a per-call timeout and a per-call budget. `--dry-run` prints the plan
  and an estimate from recorded usage and makes no calls.
- **A usage limit is a pause, not a failure.** `UsageLimitReached` stops launching, lets in-flight calls
  finish, writes `data/runs/<run_id>/state.json` with the pending pages and the reset text, and exits 75.
  Re-running resumes: every success is in the on-disk cache, so finished pages cost nothing.
- **Held-out files are locked.** `--split heldout` needs `--unlock-heldout` and refuses unless every
  hash in `gold/heldout.lock` matches the PDFs on disk.
- **Continuation pages need the previous page.** Pages are grouped into continuation chains; a chain
  runs sequentially on one worker, so page n+1 can carry page n's printed header text. Chains run in
  parallel with each other.
"""

from __future__ import annotations

import datetime as dt
import json
import threading
import time
import tomllib
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from . import __version__, wire
from .backends.base import (
    BackendConfigError,
    BackendError,
    ExtractionRequest,
    ExtractionResponse,
    SchemaInvalidError,
    TransientBackendError,
    UsageLimitReached,
)
from .backends.cache import CachedBackend
from .ids import sha256_json, short
from .paths import PATHS
from .prompts import CarryItem, assert_clean, prompt_version, system_prompt, user_prompt
from .render import read_pages

EXTRACT_VERSION = "extract/v1"
CLASS_PRIORITY = ("collar_table", "assay_table", "lith_log", "probe_log", "certificate", "uncertain", "other")
RETRY_DELAYS_S = (20.0, 60.0)          # two transient retries, per the plan
LAUNCH_GAP_S = 4.0
WORKERS = 2
CIRCUIT_BREAK_AFTER = 3                 # consecutive unknown failures
EXIT_USAGE_LIMIT = 75

# Probe-measured per-call usage, used only when no call has been recorded yet (see estimate_cost).
PROBE_FIXTURE = PATHS.pipeline / "tests" / "fixtures" / "replay" / "probe_drilllog_p1.json"


# --------------------------------------------------------------------------------- config

@dataclass(frozen=True)
class Config:
    id: str
    model: str
    effort: str
    page_classes: tuple[str, ...]
    max_pages_per_file: int
    reask: bool
    validator_mode: str          # "enforce" | "shadow"
    max_budget_usd: float
    timeout_s: int
    class_priority: tuple[str, ...]
    note: str = ""
    workers: int = WORKERS

    def as_dict(self) -> dict[str, Any]:
        return {
            "id": self.id, "model": self.model, "effort": self.effort,
            "page_classes": list(self.page_classes), "max_pages_per_file": self.max_pages_per_file,
            "reask": self.reask, "validator_mode": self.validator_mode,
            "max_budget_usd": self.max_budget_usd, "timeout_s": self.timeout_s,
            "class_priority": list(self.class_priority), "note": self.note, "workers": self.workers,
        }


def configs_dir() -> Path:
    return PATHS.pipeline / "configs"


def load_config(config_id: str) -> Config:
    path = configs_dir() / f"{config_id}.toml"
    if not path.is_file():
        have = sorted(p.stem for p in configs_dir().glob("*.toml"))
        raise FileNotFoundError(f"no config {config_id!r} in {configs_dir()} (have: {have})")
    raw = tomllib.loads(path.read_text())
    ex = raw.get("extract", raw)
    return Config(
        id=config_id,
        model=ex["model"],
        effort=ex.get("effort", "medium"),
        page_classes=tuple(ex.get("page_classes", ("collar_table", "assay_table", "lith_log"))),
        max_pages_per_file=int(ex.get("max_pages_per_file", 12)),
        reask=bool(ex.get("reask", False)),
        validator_mode=ex.get("validator_mode", "enforce"),
        max_budget_usd=float(ex.get("max_budget_usd", 0.60)),
        timeout_s=int(ex.get("timeout_s", 420)),
        class_priority=tuple(ex.get("class_priority", CLASS_PRIORITY)),
        note=ex.get("note", ""),
        workers=max(1, int(ex.get("workers", WORKERS))),
    )


# --------------------------------------------------------------------------------- plan

@dataclass(frozen=True)
class PlannedPage:
    page_id: str
    file_num: str
    pdf_sha256: str
    pdf_name: str
    page_no: int
    image_path: str
    image_sha256: str
    route_class: str
    route_candidates: tuple[str, ...]
    chain_id: str | None
    chain_pos: int | None
    width_px: int
    height_px: int
    dpi: int
    reason: str = ""

    @property
    def image_abs(self) -> Path:
        return PATHS.data / self.image_path

    @property
    def group_key(self) -> str:
        return f"{self.pdf_sha256}:{self.chain_id}" if self.chain_id else f"{self.pdf_sha256}:solo:{self.page_no}"


def heldout_files(lock_path: Path | None = None) -> set[str]:
    path = lock_path or (PATHS.gold / "heldout.lock")
    if not path.is_file():
        return set()
    return {e["file_num"] for e in json.loads(path.read_text()).get("heldout", [])}


def check_heldout_lock(lock_path: Path | None = None) -> list[str]:
    """Re-hash the locked PDFs. Returns a list of problems; empty means the lock still holds."""
    from .ids import sha256_file

    path = lock_path or (PATHS.gold / "heldout.lock")
    if not path.is_file():
        return [f"no held-out lock at {path}"]
    problems: list[str] = []
    for entry in json.loads(path.read_text()).get("heldout", []):
        for pdf in entry.get("report_pdfs", []):
            p = PATHS.raw / pdf["path"]
            if not p.is_file():
                problems.append(f"{entry['file_num']}: {pdf['path']} missing on disk")
                continue
            digest = sha256_file(p)
            if digest != pdf["sha256"]:
                problems.append(f"{entry['file_num']}: {pdf['path']} sha256 {digest[:12]} != locked {pdf['sha256'][:12]}")
    return problems


def _class_rank(cls: str, priority: tuple[str, ...]) -> int:
    return priority.index(cls) if cls in priority else len(priority)


def build_plan(config: Config, split: str = "dev", files: list[str] | None = None,
               pages_limit: int | None = None, max_pages_per_file: int | None = None,
               rows: list[dict[str, Any]] | None = None) -> list[PlannedPage]:
    """Choose pages: routed to a wanted class, rendered, not large format, inside the split."""
    rows = rows if rows is not None else read_pages()
    locked = heldout_files()
    per_file_cap = max_pages_per_file if max_pages_per_file is not None else config.max_pages_per_file
    wanted_files = {f.strip() for f in (files or []) if f.strip()}

    by_file: dict[str, list[dict[str, Any]]] = {}
    for r in rows:
        if not r.get("rendered") or r.get("large_format"):
            continue
        fn = r["file_num"]
        if split == "dev" and fn in locked:
            continue
        if split == "heldout" and fn not in locked:
            continue
        if wanted_files and fn not in wanted_files:
            continue
        cls = r.get("route_class") or "other"
        # route.py writes route_candidates as a comma-separated string ("" or NULL when there are none)
        cands = [c.strip() for c in str(r.get("route_candidates") or "").split(",") if c.strip()]
        if cls not in config.page_classes and not (set(cands) & set(config.page_classes)):
            continue
        by_file.setdefault(fn, []).append({**r, "_cands": cands})

    plan: list[PlannedPage] = []
    for fn in sorted(by_file):
        cand_rows = by_file[fn]
        # prefer the wanted classes in order, then pages inside a continuation chain, then page order
        cand_rows.sort(key=lambda r: (_class_rank(r.get("route_class") or "other", config.class_priority),
                                      0 if r.get("chain_id") else 1, int(r["page_no"])))
        chosen: dict[int, dict[str, Any]] = {}
        # reserve room for one continuation pair: a page that carries a header and the page that leans
        # on it is the only way to test the carry, and every file has at least one such chain
        chains: dict[str, list[dict[str, Any]]] = {}
        for r in cand_rows:
            if r.get("chain_id"):
                chains.setdefault(str(r["chain_id"]), []).append(r)
        pairs = sorted((c for c in chains.values() if len(c) >= 2),
                       key=lambda c: (_class_rank(c[0].get("route_class") or "other", config.class_priority),
                                      -len(c), int(c[0]["page_no"])))
        if pairs and per_file_cap >= 2:
            pair = sorted(pairs[0], key=lambda r: int(r["page_no"]))[:2]
            for r in pair:
                chosen[int(r["page_no"])] = r
        for r in cand_rows:
            if len(chosen) >= per_file_cap:
                break
            chosen[int(r["page_no"])] = r
        # complete continuation pairs: a page with chain_pos > 1 needs its predecessor for the carry
        by_page = {int(r["page_no"]): r for r in cand_rows}
        for page_no, r in list(chosen.items()):
            pos = r.get("chain_pos")
            prev = by_page.get(page_no - 1)
            if pos and int(pos) > 1 and prev is not None and page_no - 1 not in chosen:
                if len(chosen) < per_file_cap:
                    chosen[page_no - 1] = prev
                    continue
                # make room by dropping the lowest-priority solo page
                droppable = sorted(
                    (p for p, rr in chosen.items() if not rr.get("chain_id")),
                    key=lambda p: -_class_rank(chosen[p].get("route_class") or "other", config.class_priority))
                if droppable:
                    chosen.pop(droppable[0])
                    chosen[page_no - 1] = prev
        for page_no in sorted(chosen):
            r = chosen[page_no]
            plan.append(PlannedPage(
                page_id=r["page_id"], file_num=fn, pdf_sha256=r["pdf_sha256"], pdf_name=r["pdf_name"],
                page_no=int(r["page_no"]), image_path=r["image_path"], image_sha256=r["image_sha256"],
                route_class=r.get("route_class") or "other", route_candidates=tuple(r["_cands"]),
                chain_id=r.get("chain_id"), chain_pos=int(r["chain_pos"]) if r.get("chain_pos") else None,
                width_px=int(r["width_px"]), height_px=int(r["height_px"]), dpi=int(r["dpi"]),
                reason=f"routed {r.get('route_class')}" + (f", chain {r.get('chain_id')} pos {r.get('chain_pos')}" if r.get("chain_id") else ""),
            ))
    if pages_limit is not None:
        # keep whole files in plan order, but never more than the limit
        plan = plan[:pages_limit]
    return plan


# --------------------------------------------------------------------------------- carry context

def carry_from_result(result: dict[str, Any], page_no: int) -> list[CarryItem]:
    """The printed header text a continuation page may lean on: hole id, column names and units."""
    items: list[CarryItem] = []
    pl = result.get("page_level") or {}
    hole = pl.get("hole_id") or {}
    if hole.get("printed") == "printed" and hole.get("value_as_printed"):
        items.append(CarryItem("hole identifier", str(hole["value_as_printed"]), page_no,
                               str(hole.get("quote") or hole["value_as_printed"])))
    for key, label in (("depth_unit", "depth unit"), ("unit_notes", "page unit note"),
                       ("species", "grade species"), ("basis", "grade basis"), ("method", "method"),
                       ("datum", "datum"), ("utm_zone", "UTM zone")):
        f = pl.get(key) or {}
        if f.get("printed") == "printed" and f.get("value_as_printed"):
            items.append(CarryItem(label, str(f["value_as_printed"]), page_no,
                                   str(f.get("quote") or f["value_as_printed"])))
    for table in result.get("tables") or []:
        headers = [h for h in (table.get("column_headers_as_printed") or []) if h]
        if headers:
            items.append(CarryItem("column headers", " | ".join(headers), page_no,
                                   str(table.get("title_as_printed") or " | ".join(headers))))
    return items[:12]


def context_hash(carry: list[CarryItem]) -> str:
    return short(sha256_json([c.as_dict() for c in carry])) if carry else ""


# --------------------------------------------------------------------------------- run log

def run_dir(run_id: str) -> Path:
    return PATHS.runs / run_id


def new_run_id(config_id: str) -> str:
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{config_id}"


class RunLog:
    """manifest.json plus two append-only JSONL files (queried later with DuckDB)."""

    def __init__(self, run_id: str):
        self.run_id = run_id
        self.dir = run_dir(run_id)
        self.dir.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def _append(self, name: str, row: dict[str, Any]) -> None:
        with self._lock:
            with (self.dir / name).open("a") as f:
                f.write(json.dumps({"run_id": self.run_id, **row}, separators=(",", ":"), default=str) + "\n")

    def event(self, event_kind: str, **fields: Any) -> None:
        self._append("events.jsonl", {"at": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"),
                                      "event": event_kind, **fields})

    def call(self, **fields: Any) -> None:
        self._append("calls.jsonl", {"at": dt.datetime.now(dt.UTC).isoformat(timespec="milliseconds"), **fields})

    def manifest(self, obj: dict[str, Any]) -> None:
        (self.dir / "manifest.json").write_text(json.dumps(obj, indent=2, default=str) + "\n")

    def state(self, obj: dict[str, Any]) -> None:
        (self.dir / "state.json").write_text(json.dumps(obj, indent=2, default=str) + "\n")


# --------------------------------------------------------------------------------- results store

def results_path() -> Path:
    return PATHS.out / "extract" / "results.jsonl"


def read_results(path: Path | None = None) -> dict[str, dict[str, Any]]:
    p = path or results_path()
    if not p.is_file():
        return {}
    out: dict[str, dict[str, Any]] = {}
    for line in p.read_text().splitlines():
        if line.strip():
            row = json.loads(line)
            out[row["page_id"]] = row
    return out


def write_results(rows: dict[str, dict[str, Any]], path: Path | None = None) -> Path:
    p = path or results_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".part")
    with tmp.open("w") as f:
        for key in sorted(rows):
            f.write(json.dumps(rows[key], separators=(",", ":"), default=str) + "\n")
    tmp.replace(p)
    return p


# --------------------------------------------------------------------------------- cost estimate

def _usage_tokens(usage: dict[str, Any]) -> dict[str, int]:
    return {
        "input": int(usage.get("input_tokens") or 0),
        "cache_creation": int(usage.get("cache_creation_input_tokens") or 0),
        "cache_read": int(usage.get("cache_read_input_tokens") or 0),
        "output": int(usage.get("output_tokens") or 0),
    }


def recorded_usage(cache_root: Path | None = None, model: str | None = None) -> list[dict[str, Any]]:
    """Every successful call record on disk (plus the checked-in probe fixture), for the estimate."""
    from .backends.cache import calls_dir

    out: list[dict[str, Any]] = []
    seen: set[str] = set()
    for base in (calls_dir(cache_root), PROBE_FIXTURE.parent):
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.json")):
            try:
                rec = json.loads(p.read_text())
            except (json.JSONDecodeError, OSError):
                continue
            resp = rec.get("response") or {}
            if not isinstance(resp.get("structured"), dict) or rec.get("cache_key") in seen:
                continue
            if model and resp.get("model_requested") not in (None, "", model):
                continue
            seen.add(rec.get("cache_key"))
            out.append({"tokens": _usage_tokens(resp.get("usage") or {}),
                        "cost_usd": resp.get("cost_usd") or 0.0,
                        "duration_s": resp.get("duration_s") or 0.0,
                        "model": resp.get("model_requested"), "source": str(p)})
    return out


def estimate_cost(n_calls: int, cache_root: Path | None = None, model: str | None = None) -> dict[str, Any]:
    samples = recorded_usage(cache_root, model) or recorded_usage(cache_root, None)
    if not samples:
        return {"n_calls": n_calls, "basis": "no recorded call: no estimate possible", "samples": 0}
    n = len(samples)
    mean = {k: sum(s["tokens"][k] for s in samples) / n for k in ("input", "cache_creation", "cache_read", "output")}
    mean_cost = sum(s["cost_usd"] for s in samples) / n
    mean_dur = sum(s["duration_s"] for s in samples) / n
    return {
        "n_calls": n_calls,
        "samples": n,
        "basis": f"mean of {n} recorded call(s)",
        "mean_tokens_per_call": {k: round(v) for k, v in mean.items()},
        "mean_cost_usd": round(mean_cost, 4),
        "mean_duration_s": round(mean_dur, 1),
        "total_tokens": {k: round(v * n_calls) for k, v in mean.items()},
        "total_cost_usd": round(mean_cost * n_calls, 2),
        "wall_clock_estimate_s": round(max(mean_dur * n_calls / WORKERS, LAUNCH_GAP_S * n_calls)),
    }


# --------------------------------------------------------------------------------- the scheduler

def _is_budget_error(e: BaseException) -> bool:
    return "budget" in str(e).lower()


@dataclass
class _Shared:
    """Mutable scheduler state guarded by one lock (the scheduler is small; one lock is clearer)."""

    lock: threading.Lock = field(default_factory=threading.Lock)
    stop: threading.Event = field(default_factory=threading.Event)
    next_launch: float = 0.0
    calls_launched: int = 0
    consecutive_unknown: int = 0
    usage_limit: UsageLimitReached | None = None
    circuit_broken: bool = False


class Scheduler:
    def __init__(self, config: Config, backend: Any, log: Callable[[str], None] = print,
                 max_calls: int = 120, run_id: str | None = None, launch_gap_s: float = LAUNCH_GAP_S,
                 workers: int = WORKERS, retry_delays_s: tuple[float, ...] = RETRY_DELAYS_S):
        self.config = config
        self.backend = backend
        self.log = log
        self.max_calls = max_calls
        self.run_id = run_id or new_run_id(config.id)
        self.runlog = RunLog(self.run_id)
        self.launch_gap_s = launch_gap_s
        self.workers = workers
        self.retry_delays_s = retry_delays_s
        self.shared = _Shared()
        self.results: dict[str, dict[str, Any]] = {}
        self.done: set[str] = set()
        self.failed: list[dict[str, Any]] = []
        self.pending: list[str] = []
        self.system = system_prompt()
        self.prompt_version = prompt_version()
        self.schema = wire.WIRE_SCHEMA
        self.schema_version = wire.SCHEMA_VERSION
        self.totals = {"calls": 0, "cached": 0, "cost_usd": 0.0, "input": 0, "cache_creation": 0,
                       "cache_read": 0, "output": 0, "duration_s": 0.0}

    # ---- one request

    def request_for(self, page: PlannedPage, carry: list[CarryItem], reask_note: str = "") -> ExtractionRequest:
        classes = [page.route_class, *(c for c in page.route_candidates if c != page.route_class)]
        prompt = user_prompt([c for c in classes if c not in ("other", "uncertain")] or list(page.route_candidates), carry)
        if reask_note:
            prompt = f"{prompt}\n\n{reask_note}"
        assert_clean(prompt, page.file_num)
        return ExtractionRequest(
            task="extract_page",
            images=(page.image_abs,),
            system_prompt=self.system,
            user_prompt=prompt,
            schema=self.schema,
            schema_version=self.schema_version,
            prompt_version=self.prompt_version,
            model=self.config.model,
            effort=self.config.effort,
            context_hash=context_hash(carry) + ("|reask" if reask_note else ""),
            render_params={"dpi": page.dpi, "mode": "gray", "tool": "pdftoppm",
                           "width_px": page.width_px, "height_px": page.height_px},
        )

    def _wait_for_slot(self, page: PlannedPage) -> bool:
        """Reserve a launch slot: honour the gap, the cap, the stop flag. False means do not launch."""
        while True:
            with self.shared.lock:
                if self.shared.stop.is_set():
                    return False
                if self.shared.calls_launched >= self.max_calls:
                    return False
                wait = self.shared.next_launch - time.monotonic()
                if wait <= 0:
                    self.shared.calls_launched += 1
                    self.shared.next_launch = time.monotonic() + self.launch_gap_s
                    self.runlog.event("launch", page_id=page.page_id, file_num=page.file_num,
                                      page_no=page.page_no, n_launched=self.shared.calls_launched)
                    return True
            time.sleep(min(wait, 0.5))

    def _call_once(self, req: ExtractionRequest, page: PlannedPage, attempt: int) -> ExtractionResponse:
        cached = self.backend.cached(req) if hasattr(self.backend, "cached") else None
        if cached is not None:
            self.runlog.event("cache_hit", page_id=page.page_id, cache_key=cached.cache_key)
            return cached
        if not self._wait_for_slot(page):
            raise _StopLaunching()
        return self.backend.call(req, attempt=attempt) if isinstance(self.backend, CachedBackend) else self.backend.call(req)

    def extract_page(self, page: PlannedPage, carry: list[CarryItem]) -> dict[str, Any] | None:
        """Run one page through retries. Returns the wire result dict, or None if it failed."""
        reask_note = ""
        attempt = 0
        transient_left = len(self.retry_delays_s)
        schema_left = 1
        while True:
            attempt += 1
            req = self.request_for(page, carry, reask_note)
            t0 = time.monotonic()
            try:
                resp = self._call_once(req, page, attempt)
                parsed = wire.parse(resp.structured)
            except _StopLaunching:
                return None
            except UsageLimitReached as e:
                with self.shared.lock:
                    self.shared.usage_limit = self.shared.usage_limit or e
                    self.shared.stop.set()
                self.runlog.event("usage_limit", page_id=page.page_id, message=str(e)[:300],
                                  resets_at_text=e.resets_at_text)
                self.log(f"  usage limit reached on {page.file_num} p{page.page_no}: stopping launches")
                return None
            except (SchemaInvalidError, ValidationError) as e:
                self._log_failure(page, req, attempt, "schema_invalid", e, time.monotonic() - t0)
                if isinstance(self.backend, CachedBackend):
                    # an answer that does not validate is not a usable success: never keep it
                    self.backend.invalidate(req, e, attempt)
                if schema_left > 0:
                    schema_left -= 1
                    self.runlog.event("retry", page_id=page.page_id, reason="schema_invalid", attempt=attempt)
                    continue
                self._count_unknown(page, "schema_invalid")
                return None
            except TransientBackendError as e:
                kind = "budget" if _is_budget_error(e) else "transient"
                self._log_failure(page, req, attempt, kind, e, time.monotonic() - t0)
                if kind == "transient" and transient_left > 0:
                    delay = self.retry_delays_s[len(self.retry_delays_s) - transient_left]
                    transient_left -= 1
                    self.runlog.event("retry", page_id=page.page_id, reason="transient", attempt=attempt,
                                      delay_s=delay)
                    if self.shared.stop.wait(delay):
                        return None
                    continue
                self._count_unknown(page, kind)
                return None
            except BackendConfigError as e:
                # the image was never read, or a permission was denied: the answer cannot be trusted
                self._log_failure(page, req, attempt, "config", e, time.monotonic() - t0)
                self._count_unknown(page, "config")
                return None
            except BackendError as e:
                self._log_failure(page, req, attempt, "backend", e, time.monotonic() - t0)
                self._count_unknown(page, "backend")
                return None

            result = parsed.model_dump()
            self._record_success(page, req, resp, result, attempt)
            with self.shared.lock:
                self.shared.consecutive_unknown = 0
            if self.config.reask and not reask_note:
                note = self._reask_note(result)
                if note:
                    self.runlog.event("reask", page_id=page.page_id, reason=note[:160])
                    reask_note = note
                    transient_left = len(self.retry_delays_s)
                    schema_left = 1
                    continue
            return result

    @staticmethod
    def _reask_note(result: dict[str, Any]) -> str:
        gaps = []
        for t in result.get("tables") or []:
            printed = int(t.get("printed_row_count") or 0)
            stored = len(t.get("rows") or [])
            if t.get("truncated") or printed > stored:
                gaps.append(f"table {t.get('table_index')} ({t.get('kind')}): you counted {printed} printed "
                            f"rows and returned {stored}")
        if not gaps:
            return ""
        return ("Row counts do not match what you returned: " + "; ".join(gaps) +
                ". Read the image again and return every printed row of those tables, in printed order. "
                "If a row really is not there, correct printed_row_count instead.")

    def _record_success(self, page: PlannedPage, req: ExtractionRequest, resp: ExtractionResponse,
                        result: dict[str, Any], attempt: int) -> None:
        tok = _usage_tokens(resp.usage)
        n_tables = len(result.get("tables") or [])
        n_rows = sum(len(t.get("rows") or []) for t in result.get("tables") or [])
        printed_rows = sum(int(t.get("printed_row_count") or 0) for t in result.get("tables") or [])
        with self.shared.lock:
            self.totals["calls"] += 0 if resp.from_cache else 1
            self.totals["cached"] += 1 if resp.from_cache else 0
            if not resp.from_cache:
                self.totals["cost_usd"] += resp.cost_usd or 0.0
                self.totals["duration_s"] += resp.duration_s or 0.0
                for k, v in tok.items():
                    self.totals[k] += v
            self.done.add(page.page_id)
        self.results[page.page_id] = {
            "version": EXTRACT_VERSION,
            "page_id": page.page_id, "file_num": page.file_num, "pdf_sha256": page.pdf_sha256,
            "pdf_name": page.pdf_name, "page_no": page.page_no, "image_path": page.image_path,
            "image_sha256": page.image_sha256, "route_class": page.route_class,
            "chain_id": page.chain_id, "chain_pos": page.chain_pos,
            "run_id": self.run_id, "config_id": self.config.id, "cache_key": resp.cache_key,
            "model_requested": resp.model_requested, "model_resolved": resp.model_resolved,
            "prompt_version": self.prompt_version, "schema_version": self.schema_version,
            "extracted_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "from_cache": resp.from_cache, "num_turns": resp.num_turns, "duration_s": resp.duration_s,
            "usage": resp.usage, "cost_usd": resp.cost_usd, "attempt": attempt,
            "result": result,
        }
        self.runlog.call(status="cached" if resp.from_cache else "ok", page_id=page.page_id,
                         file_num=page.file_num, page_no=page.page_no, cache_key=resp.cache_key,
                         attempt=attempt, model_resolved=resp.model_resolved, num_turns=resp.num_turns,
                         duration_s=resp.duration_s, cost_usd=resp.cost_usd, tokens=tok,
                         n_tables=n_tables, n_rows=n_rows, printed_rows=printed_rows,
                         page_kind=(result.get("page_level") or {}).get("page_kind"))
        self.log(f"  {page.file_num} p{page.page_no} [{page.route_class}] "
                 f"{'cache' if resp.from_cache else 'call'}: {n_tables} tables, {n_rows}/{printed_rows} rows"
                 + ("" if resp.from_cache else f", {resp.duration_s:.0f} s, ${resp.cost_usd or 0:.3f}"))

    def _log_failure(self, page: PlannedPage, req: ExtractionRequest, attempt: int, kind: str,
                     error: BaseException, duration_s: float) -> None:
        self.runlog.call(status="failed", page_id=page.page_id, file_num=page.file_num,
                         page_no=page.page_no, cache_key=req.cache_key(self.backend.family),
                         attempt=attempt, failure_kind=kind, error_class=type(error).__name__,
                         error=str(error)[:500], duration_s=round(duration_s, 2))
        self.log(f"  {page.file_num} p{page.page_no}: {kind} failure ({type(error).__name__}): {str(error)[:160]}")

    def _count_unknown(self, page: PlannedPage, kind: str) -> None:
        with self.shared.lock:
            self.failed.append({"page_id": page.page_id, "file_num": page.file_num,
                                "page_no": page.page_no, "kind": kind})
            self.shared.consecutive_unknown += 1
            if self.shared.consecutive_unknown >= CIRCUIT_BREAK_AFTER and not self.shared.stop.is_set():
                self.shared.circuit_broken = True
                self.shared.stop.set()
                self.runlog.event("circuit_break", consecutive=self.shared.consecutive_unknown, kind=kind)
                self.log(f"  circuit breaker: {self.shared.consecutive_unknown} consecutive unknown failures, stopping")

    # ---- groups

    def run_group(self, pages: list[PlannedPage], prior: dict[str, dict[str, Any]]) -> None:
        carry: list[CarryItem] = []
        for page in pages:
            if self.shared.stop.is_set() or self.shared.calls_launched >= self.max_calls:
                with self.shared.lock:
                    self.pending.append(page.page_id)
                continue
            use_carry = carry if (page.chain_pos or 1) > 1 else []
            prior_row = prior.get(page.page_id)
            result = None
            if prior_row and prior_row.get("result") and prior_row.get("prompt_version") == self.prompt_version \
                    and prior_row.get("schema_version") == self.schema_version:
                # already extracted with this prompt and schema: reuse (the cache would return it anyway)
                with self.shared.lock:
                    self.results[page.page_id] = prior_row
                    self.done.add(page.page_id)
                    self.totals["cached"] += 1
                result = prior_row["result"]
                self.runlog.event("reused", page_id=page.page_id, run_id_prev=prior_row.get("run_id"))
            else:
                result = self.extract_page(page, use_carry)
            if result is None:
                with self.shared.lock:
                    self.pending.append(page.page_id)
                if self.shared.stop.is_set():
                    continue
                carry = []
                continue
            carry = carry_from_result(result, page.page_no)

    def run(self, plan: list[PlannedPage]) -> dict[str, Any]:
        prior = read_results()
        groups: dict[str, list[PlannedPage]] = {}
        for page in plan:
            groups.setdefault(page.group_key, []).append(page)
        for key in groups:
            groups[key].sort(key=lambda p: p.page_no)
        started = dt.datetime.now(dt.UTC)
        t0 = time.monotonic()
        self.runlog.event("plan", n_pages=len(plan), n_groups=len(groups), max_calls=self.max_calls,
                          files=sorted({p.file_num for p in plan}))
        with ThreadPoolExecutor(max_workers=self.workers, thread_name_prefix="lr-extract") as pool:
            futures = [pool.submit(self.run_group, g, prior) for g in groups.values()]
            for f in futures:
                f.result()
        wall = time.monotonic() - t0

        merged = {**prior, **self.results}
        write_results(merged)
        limit = self.shared.usage_limit
        summary = {
            "run_id": self.run_id, "version": EXTRACT_VERSION, "pipeline_version": __version__,
            "config": self.config.as_dict(), "prompt_version": self.prompt_version,
            "schema_version": self.schema_version, "backend_family": self.backend.family,
            "started_at": started.isoformat(timespec="seconds"),
            "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "wall_s": round(wall, 1),
            "planned": len(plan), "done": len(self.done), "failed": len(self.failed),
            "pending": sorted(set(self.pending) - set(self.done)),
            "totals": {**self.totals, "cost_usd": round(self.totals["cost_usd"], 4)},
            "usage_limit": {"message": str(limit)[:300], "resets_at_text": limit.resets_at_text} if limit else None,
            "circuit_broken": self.shared.circuit_broken,
            "max_calls_reached": self.shared.calls_launched >= self.max_calls,
            "failures": self.failed,
        }
        self.runlog.manifest(summary)
        if summary["pending"]:
            by_id = {p.page_id: p for p in plan}
            self.runlog.state({
                "run_id": self.run_id, "config_id": self.config.id,
                "reason": "usage_limit" if limit else ("circuit_break" if self.shared.circuit_broken
                                                       else "max_calls" if summary["max_calls_reached"] else "failures"),
                "resets_at_text": limit.resets_at_text if limit else None,
                "pending_pages": [{"page_id": pid, "file_num": by_id[pid].file_num,
                                   "page_no": by_id[pid].page_no, "route_class": by_id[pid].route_class}
                                  for pid in summary["pending"] if pid in by_id],
                "resume": f"lr extract --config {self.config.id}",
            })
        return summary


class _StopLaunching(Exception):
    """Internal: the cap or the stop flag was reached before this page launched."""


# --------------------------------------------------------------------------------- entry point

def stage_extract(config_id: str = "phase2", split: str = "dev", files: list[str] | None = None,
                  max_calls: int = 120, pages_limit: int | None = None, max_pages_per_file: int | None = None,
                  dry_run: bool = False, unlock_heldout: bool = False, backend: Any = None,
                  log: Callable[[str], None] = print, launch_gap_s: float = LAUNCH_GAP_S) -> dict[str, Any]:
    config = load_config(config_id)
    if split == "heldout":
        if not unlock_heldout:
            raise PermissionError("--split heldout needs --unlock-heldout (held-out files are read once, after the freeze)")
        problems = check_heldout_lock()
        if problems:
            raise PermissionError("held-out lock does not match what is on disk: " + "; ".join(problems))
    plan = build_plan(config, split=split, files=files, pages_limit=pages_limit,
                      max_pages_per_file=max_pages_per_file)
    by_file: dict[str, list[PlannedPage]] = {}
    for p in plan:
        by_file.setdefault(p.file_num, []).append(p)

    log(f"config {config.id}: model {config.model}, effort {config.effort}, classes {list(config.page_classes)}, "
        f"re-ask {'on' if config.reask else 'off'}, validators {config.validator_mode}")
    log(f"prompt {prompt_version()}  schema {wire.SCHEMA_VERSION}  split {split}")
    log(f"plan: {len(plan)} pages over {len(by_file)} files (cap {max_calls} calls)")
    for fn in sorted(by_file):
        pages = by_file[fn]
        classes = ", ".join(f"{c}:{sum(1 for p in pages if p.route_class == c)}"
                            for c in sorted({p.route_class for p in pages}))
        chains = sorted({p.chain_id for p in pages if p.chain_id})
        log(f"  {fn}: {len(pages)} pages [{classes}]"
            + (f", {len(chains)} continuation chain(s)" if chains else "")
            + f"  pages {[p.page_no for p in pages]}")

    n_to_call = len([p for p in plan if p.page_id not in read_results()])
    est = estimate_cost(min(n_to_call, max_calls), model=config.model)
    log("estimate (" + str(est.get("basis")) + "): "
        + json.dumps({k: est[k] for k in ("n_calls", "total_tokens", "total_cost_usd", "wall_clock_estimate_s")
                      if k in est}))
    if dry_run:
        return {"dry_run": True, "config": config.as_dict(), "split": split, "planned": len(plan),
                "by_file": {k: [p.page_no for p in v] for k, v in by_file.items()},
                "already_extracted": len(plan) - n_to_call, "estimate": est,
                "plan": [{"page_id": p.page_id, "file_num": p.file_num, "page_no": p.page_no,
                          "route_class": p.route_class, "chain_id": p.chain_id, "chain_pos": p.chain_pos}
                         for p in plan]}

    if backend is None:
        from .backends.claude_cli import ClaudeCliBackend
        backend = CachedBackend(ClaudeCliBackend(timeout_s=config.timeout_s,
                                                 max_budget_usd=config.max_budget_usd))
    scheduler = Scheduler(config, backend, log=log, max_calls=max_calls, launch_gap_s=launch_gap_s,
                          workers=config.workers)
    summary = scheduler.run(plan)
    summary["estimate"] = est
    log(f"\nrun {summary['run_id']}: {summary['done']} pages done, {summary['failed']} failed, "
        f"{len(summary['pending'])} pending; {summary['totals']['calls']} calls "
        f"({summary['totals']['cached']} from cache), ${summary['totals']['cost_usd']:.3f}, "
        f"{summary['wall_s']:.0f} s wall")
    if summary["usage_limit"]:
        log(f"usage limit: {summary['usage_limit']['message']}")
        log(f"resume with: lr extract --config {config.id}")
    return summary
