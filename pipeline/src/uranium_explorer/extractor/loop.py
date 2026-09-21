"""The run: every planned page through locate → read → validate → agree → file, budgeted and resumable.

What a run guarantees, in the order a page meets them:

* **Nothing is read twice.** A page whose first-family reading is already in the batch reader's results file
  (the same prompt and schema versions) is carried in as read at no cost, and a page already assembled and
  validated on disk is carried in as validated; the loop then runs the stages it has not seen. With
  `agree_only` the reader is never called at all: a page with no first reading is left pending and said.
* **The run names itself before it spends.** A `Manifest` is written before the first call, with the reader
  and second-family models, the prompt and schema hashes and the budget; the run is a trace, every stage a
  span, every model call a span under it (the cached backend's).
* **The budget stops it, cleanly.** Before every live call the cached backend checks the run's budget and the
  ledger's ceilings; `BudgetExhausted` or a usage limit stops the run at the page it was on, that page is
  marked stopped, the rest stay pending, and `resume=<run id>` carries every finished stage forward.
* **Every stage is on disk as it finishes.** `pages.jsonl` gets the page's record after each stage; the two
  readings of a page live under `readings/`, the comparison under `agreement/`, and the queue rows in
  `queue.jsonl`, so the run directory alone says what happened and what would be filed.
* **Filing is the last stage and the only one that writes the store.** A fresh reading lands in the batch
  reader's results file and the file's assembled document (what `ue store rebuild` files under `read`), and
  the agreement marks and queue rows go into `read.agreement` and `read.review_item` in the store the run
  was pointed at.
"""

from __future__ import annotations

import datetime as dt
import json
import shutil
from pathlib import Path
from typing import Any, Callable

from pydantic import ValidationError

from .. import wire
from ..assemble import assemble_file, assembled_path, read_assembled
from ..backends.base import (
    BackendConfigError,
    BackendError,
    ExtractionRequest,
    SchemaInvalidError,
    UsageLimitReached,
)
from ..backends.cache import CachedBackend
from ..extract import (
    Config,
    PlannedPage,
    carry_from_result,
    context_hash,
    read_results,
    write_results,
)
from ..ids import sha256_json
from ..ocr import read_words
from ..locate import PageLocator
from ..paths import PATHS
from ..prompts import CarryItem, assert_clean, prompt_version, system_prompt, user_prompt
from ..render import read_pages
from ..runtime.manifest import Manifest
from ..runtime.runs import claim_run_dir, run_dir
from ..runtime.spend import BudgetExhausted, RunBudget
from ..runtime.tracing import set_attrs, span, trace
from ..validators import (
    SHADOW_MODE_IDS,
    _load_crosschecks,
    _load_positions,
    _text_layer_by_page,
    apply_findings,
    build_context,
    run_validators,
    validators_path,
)
from ..validators.checks import geometric_row_counts
from . import agree as AG
from . import queue as Q
from .states import ORDER, PageState, Stage, append_state, read_states

KIND = "extract"
LOOP_VERSION = "extractor/v1"
DEFAULT_SECOND_MODEL = "z-ai/glm-5.3-flash"
DEFAULT_SECOND_EFFORT = "low"
#: the most one second-family call is allowed to cost, checked against the run budget before it is sent: a
#: dense page is under 20k completion tokens, which at GLM 5.3 Flash's prices is under a cent; the ceiling is
#: pessimistic on purpose so a dearer second family still stops on the budget rather than the ledger
AGREE_ESTIMATE_USD = 0.05
#: completion room for the second read: the densest Opus page produced 18.5k output tokens, and a cut answer
#: is a schema failure, not a saving
SECOND_MAX_TOKENS = 24000


class Stopped(Exception):
    """The run has to stop: a budget or a usage limit. Carries the signal that said so."""

    def __init__(self, signal: BaseException) -> None:
        super().__init__(str(signal))
        self.signal = signal


def _page_words(pdf_sha256: str, page_no: int) -> tuple[list[dict[str, Any]], str, int]:
    """The OCR words of one page and the engine chosen for them, the way the assembler chooses: Live Text
    when it read at least five words and no fewer than Vision, else Vision. Plus the text-layer word count."""
    words = [w for w in read_words(pdf_sha256) if int(w["page_no"]) == int(page_no)]
    lt = [w for w in words if w["engine"] == "livetext"]
    vis = [w for w in words if w["engine"] == "vision"]
    text_layer = sum(1 for w in words if w["engine"] == "pdftotext")
    chosen, engine = (lt, "livetext") if len(lt) >= max(5, len(vis)) else (vis, "vision")
    return chosen, engine, text_layer


class ExtractorRun:
    """One invocation of the loop over a plan. Build it, then `run()`."""

    def __init__(self, config: Config, backend: Any, plan: list[PlannedPage], *, second_model: str = DEFAULT_SECOND_MODEL,
                 second_effort: str = DEFAULT_SECOND_EFFORT, budget_usd: float | None = None,
                 store: Path | None = None, write_store: bool = True, agree_only: bool = False,
                 skip_agree: bool = False, resume: str | None = None, log: Callable[[str], None] = print,
                 cache_root: Path | None = None, run_id: str | None = None) -> None:
        self.config = config
        self.plan = list(plan)
        self.second_model = second_model
        self.second_effort = second_effort
        self.agree_only = agree_only
        self.skip_agree = skip_agree
        self.store_path = store
        self.write_store = write_store
        self.log = log
        self.system = system_prompt()
        self.prompt_version = prompt_version()
        self.schema = wire.WIRE_SCHEMA
        self.schema_version = wire.SCHEMA_VERSION
        models = {"reader": config.model}
        if not skip_agree:
            models["second"] = second_model
        self.manifest = Manifest.start(
            kind=KIND, config={**config.as_dict(), "loop": LOOP_VERSION, "agree_only": agree_only,
                               "skip_agree": skip_agree, "second_effort": second_effort,
                               "store": str(store) if store else None, "write_store": write_store},
            models=models, budget_usd=budget_usd, run_id=run_id)
        self.manifest.prompt_hashes = {f"extract/{self.prompt_version}": sha256_json(self.system)}
        self.manifest.schema_hashes = {f"wire/{self.schema_version}": sha256_json(self.schema)}
        if run_id:
            self.run_id, self.rd = run_id, run_dir(run_id)
            self.rd.mkdir(parents=True, exist_ok=True)
        else:
            self.run_id, self.rd = claim_run_dir(config.id + "-agent")
            self.manifest.run_id = self.run_id
        self.manifest.notes = f"resumed from {resume}" if resume else ""
        self.budget: RunBudget = self.manifest.budget()
        # two wrappers over one backend, one budget: the reader's per-call ceiling is the config's, the second
        # family's is the small one above; both charge the same RunBudget
        self.reader = CachedBackend(backend, root=cache_root, run_budget=self.budget, estimate_usd=config.max_budget_usd)
        self.second = CachedBackend(backend, root=cache_root, run_budget=self.budget, estimate_usd=AGREE_ESTIMATE_USD)
        self.states: dict[str, PageState] = {}
        self.results: dict[str, dict[str, Any]] = read_results()
        self.page_rows = {r["page_id"]: r for r in read_pages()}
        self.fresh_files: set[str] = set()          # files that gained a reading in this run
        self._docs: dict[str, dict[str, Any]] = {}  # file_num -> validated document
        self._locators: dict[str, PageLocator] = {}
        self._con: Any = None
        self.filed_queue: list[dict[str, Any]] = []
        self.stopped_by: BaseException | None = None
        self._resume(resume)

    # ------------------------------------------------------------------ setup

    def _resume(self, resume: str | None) -> None:
        if not resume:
            return
        old = run_dir(resume)
        carried = read_states(old)
        for page in self.plan:
            st = carried.get(page.page_id)
            if st is None:
                continue
            for sub in ("readings", "agreement"):
                for p in (old / sub).glob(f"{page.page_id.replace(':', '_')}*"):
                    (self.rd / sub).mkdir(parents=True, exist_ok=True)
                    shutil.copy2(p, self.rd / sub / p.name)
            if st.status in ("failed", "stopped"):
                st.status, st.error = "pending", None   # the stage is run again; what failed is in the old run
            self.states[page.page_id] = st
        if self.states:
            self.log(f"  {len(self.states)} page(s) carried from {resume}")

    def _state(self, page: PlannedPage) -> PageState:
        st = self.states.get(page.page_id)
        if st is None:
            st = PageState(page_id=page.page_id, file_num=page.file_num, page_no=page.page_no,
                           pdf_sha256=page.pdf_sha256, route_class=page.route_class, image_path=page.image_path)
            self.states[page.page_id] = st
        return st

    def _slug(self, page_id: str) -> str:
        return page_id.replace(":", "_")

    def _reading_path(self, page_id: str, which: str) -> Path:
        return self.rd / "readings" / f"{self._slug(page_id)}.{which}.json"

    def _write_json(self, path: Path, obj: Any) -> Path:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(obj, separators=(",", ":"), default=str))
        return path

    def _locator(self, page: PlannedPage) -> PageLocator:
        loc = self._locators.get(page.page_id)
        if loc is None:
            words, _engine, _tl = _page_words(page.pdf_sha256, page.page_no)
            loc = PageLocator(words)
            self._locators[page.page_id] = loc
        return loc

    # ------------------------------------------------------------------ requests

    def _request(self, page: PlannedPage, model: str, effort: str, carry: list[CarryItem]) -> ExtractionRequest:
        classes = [page.route_class, *(c for c in page.route_candidates if c != page.route_class)]
        prompt = user_prompt([c for c in classes if c not in ("other", "uncertain")] or list(page.route_candidates), carry)
        assert_clean(prompt, page.file_num)
        return ExtractionRequest(
            task="extract_page", images=(page.image_abs,), system_prompt=self.system, user_prompt=prompt,
            schema=self.schema, schema_version=self.schema_version, prompt_version=self.prompt_version,
            model=model, effort=effort, context_hash=context_hash(carry),
            render_params={"dpi": page.dpi, "mode": "gray", "tool": "pdftoppm",
                           "width_px": page.width_px, "height_px": page.height_px},
        )

    def _call(self, backend: CachedBackend, req: ExtractionRequest) -> tuple[dict[str, Any], Any]:
        """One model call through the cache, parsed against the wire schema. A schema-invalid answer is
        dropped from the cache and the call raised, never kept."""
        try:
            resp = backend.call(req)
            parsed = wire.parse(resp.structured)
        except (BudgetExhausted, UsageLimitReached) as signal:
            raise Stopped(signal) from signal
        except ValidationError as e:
            backend.invalidate(req, e)
            raise SchemaInvalidError(f"the answer does not validate against the wire schema: {str(e)[:300]}") from e
        return parsed.model_dump(), resp

    def _carry(self, page: PlannedPage) -> list[CarryItem]:
        """The printed header of the previous page of a continued table, when that page has been read."""
        if not page.chain_id or (page.chain_pos or 1) <= 1:
            return []
        prev = next((r for r in self.results.values() if r["pdf_sha256"] == page.pdf_sha256
                     and int(r["page_no"]) == page.page_no - 1), None)
        return carry_from_result(prev["result"], page.page_no - 1) if prev else []

    # ------------------------------------------------------------------ the stages

    def stage_locate(self, page: PlannedPage, st: PageState) -> dict[str, Any]:
        words, engine, text_layer = _page_words(page.pdf_sha256, page.page_no)
        self._locators[page.page_id] = PageLocator(words)
        if not words:
            self.log(f"    {page.file_num} p{page.page_no}: no OCR words; values will carry no box")
        return {"engine": engine, "n_words": len(words), "text_layer_words": text_layer,
                "image_path": page.image_path}

    def stage_read(self, page: PlannedPage, st: PageState) -> dict[str, Any]:
        prior = self.results.get(page.page_id)
        if prior and prior.get("result") and prior.get("prompt_version") == self.prompt_version \
                and prior.get("schema_version") == self.schema_version:
            self._write_json(self._reading_path(page.page_id, "a"), prior["result"])
            return {"source": "prior", "run_id_prev": prior.get("run_id"), "model": prior.get("model_resolved"),
                    "prompt_version": prior.get("prompt_version"), "cache_key": prior.get("cache_key"),
                    "cost_usd": 0.0, "from_cache": True, "n_tables": len(prior["result"].get("tables") or [])}
        if self.agree_only:
            raise PermissionError("no first-family reading on disk for this page and the run is agree-only: "
                                  "run `ue extract` (or the loop without --agree-only) to read it first")
        req = self._request(page, self.config.model, self.config.effort, self._carry(page))
        result, resp = self._call(self.reader, req)
        row = {
            "version": "extract/v1", "page_id": page.page_id, "file_num": page.file_num,
            "pdf_sha256": page.pdf_sha256, "pdf_name": page.pdf_name, "page_no": page.page_no,
            "image_path": page.image_path, "image_sha256": page.image_sha256, "route_class": page.route_class,
            "chain_id": page.chain_id, "chain_pos": page.chain_pos, "run_id": self.run_id,
            "config_id": self.config.id, "cache_key": resp.cache_key, "model_requested": resp.model_requested,
            "model_resolved": resp.model_resolved, "prompt_version": self.prompt_version,
            "schema_version": self.schema_version,
            "extracted_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "from_cache": resp.from_cache, "num_turns": resp.num_turns, "duration_s": resp.duration_s,
            "usage": resp.usage, "cost_usd": resp.cost_usd, "attempt": 1, "result": result,
        }
        self.results[page.page_id] = row
        self.fresh_files.add(page.file_num)
        self._docs.pop(page.file_num, None)   # the file's document has to be assembled again
        self._write_json(self._reading_path(page.page_id, "a"), result)
        return {"source": "live", "model": resp.model_resolved, "prompt_version": self.prompt_version,
                "cache_key": resp.cache_key, "cost_usd": resp.cost_usd, "from_cache": resp.from_cache,
                "duration_s": resp.duration_s, "n_tables": len(result.get("tables") or [])}

    def _document(self, file_num: str) -> dict[str, Any]:
        """The file's assembled and validated document: the one on disk when nothing changed, else assembled
        from the results the run holds and validated in memory (filed by the `file` stage)."""
        doc = self._docs.get(file_num)
        if doc is not None:
            return doc
        if file_num not in self.fresh_files and assembled_path(file_num).is_file():
            doc = read_assembled(file_num)
            if doc.get("validators") is None:
                doc = self._validate_doc(file_num, doc)
        else:
            rows = [r for r in self.results.values() if r["file_num"] == file_num]
            doc = self._validate_doc(file_num, assemble_file(file_num, rows, self.page_rows, log=self.log))
        self._docs[file_num] = doc
        return doc

    def _validate_doc(self, file_num: str, doc: dict[str, Any]) -> dict[str, Any]:
        ctx = build_context(file_num)
        ctx["positions"] = _load_positions(file_num)
        ctx["crosschecks"] = _load_crosschecks(file_num)
        ctx["geometric_rows"] = geometric_row_counts(doc)
        ctx["text_layer"] = _text_layer_by_page(doc)
        shadow = SHADOW_MODE_IDS if self.config.validator_mode == "shadow" else frozenset()
        return apply_findings(doc, run_validators(doc, ctx), shadow=shadow)

    def stage_validate(self, page: PlannedPage, st: PageState) -> dict[str, Any]:
        doc = self._document(page.file_num)
        meta = doc.get("value_meta") or {}
        on_page = {vid for vid, m in meta.items() if int(m.get("page") or 0) == page.page_no}
        findings = [f for f in (doc.get("validators") or {}).get("findings", []) if set(f.get("value_ids") or []) & on_page]
        counts: dict[str, int] = {}
        for f in findings:
            if f["outcome"] in ("flag", "fail"):
                counts[f["validator"]] = counts.get(f["validator"], 0) + 1
        flagged = sum(1 for vid in on_page if (doc["values"].get(vid) or {}).get("status") == "flag")
        return {"source": "run" if page.file_num in self.fresh_files else "prior", "values": len(on_page),
                "findings": len(findings), "flagged_values": flagged,
                "counts": dict(sorted(counts.items())), "mode": self.config.validator_mode}

    def stage_agree(self, page: PlannedPage, st: PageState) -> dict[str, Any]:
        if self.skip_agree:
            return {"skipped": True, "reason": "run without a second family"}
        doc = self._document(page.file_num)
        a_values = AG.candidates_from_assembled(doc, page.page_no)
        model_a = (st.read or {}).get("model") or self.config.model
        req = self._request(page, self.second_model, self.second_effort, self._carry(page))
        result_b, resp = self._call(self.second, req)
        self._write_json(self._reading_path(page.page_id, "b"), result_b)
        loc = self._locator(page)
        b_values = AG.candidates_from_wire(result_b, loc, model=resp.model_resolved,
                                          prompt_version=self.prompt_version, run_id=self.run_id)
        cmp = AG.compare(a_values, b_values)
        record = {"version": AG.AGREE_VERSION, "page_id": page.page_id, "file_num": page.file_num,
                  "page": page.page_no, "model_a": model_a, "model_b": resp.model_resolved,
                  "n_a": cmp["n_a"], "n_b": cmp["n_b"], "tally": cmp["tally"],
                  "pairs": [p.as_dict() for p in cmp["pairs"]]}
        self._write_json(self.rd / "agreement" / f"{self._slug(page.page_id)}.json", record)
        t = cmp["tally"]
        self.log(f"    {page.file_num} p{page.page_no}: {t['agreed']} agreed, {t['disagreed']} disagreed, "
                 f"{t['only_a']} only first, {t['only_b']} only second "
                 f"(rate {t['rate'] if t['rate'] is not None else 'n/a'}; "
                 f"{'cache' if resp.from_cache else f'${resp.cost_usd or 0:.4f}'})")
        return {"model_b": resp.model_resolved, "model_a": model_a, "cost_usd": resp.cost_usd,
                "from_cache": resp.from_cache, "cache_key": resp.cache_key, "duration_s": resp.duration_s,
                "n_a": cmp["n_a"], "n_b": cmp["n_b"], "tally": t, "queue": len(cmp["queue"])}

    def _store(self) -> Any:
        if self._con is None:
            self._con = Q.open_store(self.store_path)
        return self._con

    def stage_file(self, page: PlannedPage, st: PageState) -> dict[str, Any]:
        block: dict[str, Any] = {"store": str(self.store_path) if self.write_store else None}
        # a fresh reading: the results file and the file's assembled document, the batch reader's own artefacts
        if page.file_num in self.fresh_files:
            write_results(self.results)
            doc = self._document(page.file_num)
            path = assembled_path(page.file_num)
            path.parent.mkdir(parents=True, exist_ok=True)
            self._write_json(path, doc)
            vp = validators_path(page.file_num)
            self._write_json(vp, {"file_num": page.file_num, "mode": self.config.validator_mode,
                                  "shadow": sorted(SHADOW_MODE_IDS if self.config.validator_mode == "shadow" else ()),
                                  "findings": (doc.get("validators") or {}).get("findings", [])})
            block["assembled"] = str(path)
        # the comparison: agreement marks and queue rows, to the run directory and to the store
        rec_path = self.rd / "agreement" / f"{self._slug(page.page_id)}.json"
        if rec_path.is_file():
            record = json.loads(rec_path.read_text())
            pairs = [AG.Pair(status=p["status"], a=AG.Candidate(**p["a"]) if p["a"] else None,
                             b=AG.Candidate(**p["b"]) if p["b"] else None, matched_by=p["matched_by"],
                             detail=p.get("detail", "")) for p in record["pairs"]]
            common = dict(run_id=self.run_id, file_num=page.file_num, page_id=page.page_id, page_no=page.page_no,
                          model_a=record.get("model_a"), model_b=record.get("model_b"))
            agreement = [Q.agreement_row(p, prompt_version=self.prompt_version, **common) for p in pairs]
            queue = [Q.queue_row(p, **common) for p in pairs if p.status != "agreed"]
            with (self.rd / "queue.jsonl").open("a") as f:
                for r in queue:
                    f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
            with (self.rd / "agreement.jsonl").open("a") as f:
                for r in agreement:
                    f.write(json.dumps(r, separators=(",", ":"), default=str) + "\n")
            self.filed_queue.extend(queue)
            block.update(agreement_rows=len(agreement), queue_rows=len(queue))
            if self.write_store:
                block["filed"] = Q.file_rows(self._store(), agreement, queue)
        block["values"] = (st.validate or {}).get("values")
        return block

    STAGES: dict[Stage, str] = {Stage.LOCATE: "stage_locate", Stage.READ: "stage_read",
                                Stage.VALIDATE: "stage_validate", Stage.AGREE: "stage_agree", Stage.FILE: "stage_file"}

    # ------------------------------------------------------------------ the loop

    def run_page(self, page: PlannedPage) -> PageState:
        st = self._state(page)
        while not st.done:
            stage = st.stage
            with span(f"page:{stage.value}", kind="tool", page_id=page.page_id, file_num=page.file_num,
                      page_no=page.page_no, route_class=page.route_class):
                try:
                    block = getattr(self, self.STAGES[stage])(page, st)
                except Stopped as stop:
                    st.stop(f"{type(stop.signal).__name__}: {stop.signal}")
                    set_attrs(status="stopped")
                    append_state(self.rd, st)
                    raise
                except (BackendError, PermissionError, RuntimeError, ValueError, OSError) as err:
                    st.fail(stage, err)
                    set_attrs(status="failed", error=str(err)[:300])
                    append_state(self.rd, st)
                    self.log(f"    {page.file_num} p{page.page_no}: {stage.value} failed: {type(err).__name__}: {str(err)[:200]}")
                    return st
                st.finish(stage, block)
                set_attrs(status="ok")
            append_state(self.rd, st)
        return st

    def run(self) -> dict[str, Any]:
        started = dt.datetime.now(dt.UTC)
        self.manifest.write(self.rd)
        self.log(f"  run {self.run_id}: {len(self.plan)} page(s), reader {self.config.model}, "
                 f"second {'none' if self.skip_agree else self.second_model}, "
                 f"budget {'none' if self.budget.cap_usd is None else f'${self.budget.cap_usd:.2f}'}"
                 f"{', agree-only' if self.agree_only else ''}")
        pending: list[str] = []
        try:
            with trace(self.run_id, KIND, run_dir=self.rd, loop=LOOP_VERSION, reader=self.config.model,
                       second=self.second_model):
                for page in self.plan:
                    if self.stopped_by is not None:
                        pending.append(page.page_id)
                        continue
                    try:
                        self.run_page(page)
                    except Stopped as stop:
                        self.stopped_by = stop.signal
                        pending.append(page.page_id)
                        self.log(f"  stopped at {page.file_num} p{page.page_no}: {stop.signal}")
        finally:
            if self._con is not None:
                self._con.close()
                self._con = None
        spent = float(self.budget.spent_usd)
        self.manifest.finish(spent_usd=spent)
        self.manifest.write(self.rd)
        states = [self.states[p.page_id] for p in self.plan if p.page_id in self.states]
        by_status = {s: sum(1 for st in states if st.status == s) for s in ("done", "pending", "failed", "stopped")}
        by_stage = {s.value: sum(1 for st in states if st.stage is s and not st.done) for s in ORDER}
        agreement = AG.merge_tallies(st.agree["tally"] for st in states if st.agree and st.agree.get("tally"))
        summary = {
            "run_id": self.run_id, "run_dir": str(self.rd), "version": LOOP_VERSION, "config": self.config.id,
            "models": dict(self.manifest.models), "prompt_version": self.prompt_version,
            "schema_version": self.schema_version, "started_at": started.isoformat(timespec="seconds"),
            "finished_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "planned": len(self.plan), **by_status, "pending_pages": sorted(set(pending)),
            "at_stage": by_stage, "spent_usd": round(spent, 6), "budget_usd": self.budget.cap_usd,
            "budget_exhausted": isinstance(self.stopped_by, BudgetExhausted),
            "usage_limited": isinstance(self.stopped_by, UsageLimitReached),
            "stopped_by": f"{type(self.stopped_by).__name__}: {self.stopped_by}" if self.stopped_by else None,
            "agreement": agreement, "queue_rows": len(self.filed_queue),
            "store": str(self.store_path) if self.write_store else None,
            "fresh_files": sorted(self.fresh_files),
            "pages": [{"page_id": st.page_id, "file_num": st.file_num, "page": st.page_no, "status": st.status,
                       "stage": st.stage.value, "error": st.error,
                       "agree": {k: v for k, v in (st.agree or {}).items() if k in ("tally", "model_b", "cost_usd", "from_cache", "queue")} or None}
                      for st in states],
        }
        (self.rd / "summary.json").write_text(json.dumps(summary, indent=1, default=str) + "\n")
        return summary


# --------------------------------------------------------------------------------- the plan

def parse_selectors(selectors: list[str]) -> list[tuple[str, int]]:
    """`<file>:<page>` selectors, so a run can name exact pages."""
    out: list[tuple[str, int]] = []
    for s in selectors:
        file_num, _, page = s.strip().rpartition(":")
        if not file_num or not page.isdigit():
            raise ValueError(f"a page selector looks like 74H16-0034:27, not {s!r}")
        out.append((file_num, int(page)))
    return out


def plan_from_results(selectors: list[tuple[str, int]], results: dict[str, dict[str, Any]] | None = None
                      ) -> list[PlannedPage]:
    """Planned pages built from the batch reader's results rows: what the loop needs of a page that has
    already been read, without going back through the router."""
    rows = results if results is not None else read_results()
    by_key = {(r["file_num"], int(r["page_no"])): r for r in rows.values()}
    pages = {r["page_id"]: r for r in read_pages()}
    out: list[PlannedPage] = []
    for file_num, page_no in selectors:
        r = by_key.get((file_num, page_no))
        if r is None:
            continue
        pr = pages.get(r["page_id"], {})
        cands = [c.strip() for c in str(pr.get("route_candidates") or "").split(",") if c.strip()]
        out.append(PlannedPage(
            page_id=r["page_id"], file_num=file_num, pdf_sha256=r["pdf_sha256"], pdf_name=r.get("pdf_name") or "",
            page_no=page_no, image_path=r["image_path"], image_sha256=r.get("image_sha256") or "",
            route_class=r.get("route_class") or "other", route_candidates=tuple(cands),
            chain_id=r.get("chain_id"), chain_pos=r.get("chain_pos"),
            width_px=int(pr.get("width_px") or 0), height_px=int(pr.get("height_px") or 0), dpi=int(pr.get("dpi") or 200),
            reason="already read",
        ))
    return out


def check_second_model_takes_images(model: str, log: Callable[[str], None] = print) -> None:
    """Refuse a second family that cannot see the page: OpenRouter's catalogue says which models take
    images. A model the catalogue does not list is refused too, rather than paid for blind."""
    from ..backends.openrouter import is_openrouter_model, list_models

    if not is_openrouter_model(model):
        return
    entry = next((m for m in list_models() if m["id"] == model), None)
    if entry is None:
        raise BackendConfigError(f"{model!r} is not in OpenRouter's model list")
    if not entry.get("vision"):
        raise BackendConfigError(f"{model!r} does not take images on OpenRouter; the second reader has to see the page")
    log(f"  second family {model}: takes images, ${entry['usd_per_mtok_in']:.2f}/${entry['usd_per_mtok_out']:.2f} per Mtok")


def default_store_path() -> Path:
    return PATHS.data / "ue.duckdb"
