"""Move cached calls recorded under the old cache key to the key a live request computes now (B22).

The old key tracked the *versions* of the prompt and schema; the new one hashes their texts as well, so a
changed system prompt can no longer be answered from an older run's cache. That makes every old record a miss
under the new key, and the extract cache holds a few hundred paid Opus reads nobody wants to buy twice.

Only `extract_page` records can be moved, and only when the record's `prompt_version` and `schema_version`
equal the current ones: those versions are hashes of the same template and schema texts, so equality proves
the texts the record was made with are the texts a live request would send. The memo and chat records stay
where they are — their prompts carry a transcript and an evidence bundle that no walk of the cache can
rebuild — and they are counted so the operator can see what was left.

One thing the old record does not carry is the rendered user prompt: the extract prompt names the page's
routed classes and, for a continuation page, the header text carried from the page before. Both are rebuilt
from the extract results (`data/out/extract/results.jsonl`) and the page index, and the rebuild is checked two
ways before anything is written: the record's own key must recompute from the record (so the layout is
understood), and the carried context must hash to the record's `context_hash` (so the carry is the one that
was sent). A record that fails either check is counted as unrebuildable and left alone. The routed class list
comes from the results row and the index; a page re-routed since it was read gets the class it was read with.

The old file is left in place. Running this twice writes nothing the second time.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from .. import wire
from ..backends.base import HASH_KEYS, cache_key_fields, hash_schema, hash_text
from ..backends.cache import RECORD_VERSION, calls_dir, record_path, write_atomic
from ..ids import sha256_json
from ..prompts import prompt_version, system_prompt, user_prompt

V1 = "call-record/v1"
#: families a v1 record may have been recorded by, tried in this order when the record does not say
FAMILIES = ("claude_cli", "openai")


def results_rows() -> dict[str, dict[str, Any]]:
    """The extract results, by page id. A function so a test can stand in its own."""
    from ..extract import read_results

    return read_results()


def page_rows() -> list[dict[str, Any]]:
    """The page index (routing classes and candidates per page). A function so a test can stand in its own."""
    from ..render import read_pages

    return read_pages()


@dataclass
class Context:
    """Everything a rebuild needs, loaded once."""

    system_hash: str
    schema_hash: str
    prompt_version: str
    schema_version: str
    by_key: dict[str, dict[str, Any]]                   # cache key -> results row
    by_page: dict[tuple[str, int], dict[str, Any]]      # (pdf sha256, page no) -> results row
    index: dict[str, dict[str, Any]]                    # page id -> index row

    @classmethod
    def load(cls) -> "Context":
        results = results_rows()
        return cls(
            system_hash=hash_text(system_prompt()), schema_hash=hash_schema(wire.WIRE_SCHEMA),
            prompt_version=prompt_version(), schema_version=wire.SCHEMA_VERSION,
            by_key={r["cache_key"]: r for r in results.values() if r.get("cache_key")},
            by_page={(r["pdf_sha256"], int(r["page_no"])): r for r in results.values()},
            index={r["page_id"]: r for r in page_rows()},
        )


# ---------------------------------------------------------------- keys from a record


def key_fields_from_record(rec: dict[str, Any], family: str, system_hash: str = "", user_hash: str = "",
                           schema_hash: str = "") -> dict[str, Any]:
    """The key dictionary a record's request summary implies, laid out exactly as a live request lays it."""
    rq = rec["request"]
    return cache_key_fields(
        task=rq["task"], backend_family=family,
        image_hashes=[i["sha256"] for i in rq.get("images") or []],
        stage_hashes=[list(s) for s in rq.get("stage_files") or []],
        render_params=rq.get("render_params") or {},
        prompt_version=rq["prompt_version"], schema_version=rq["schema_version"],
        model=rq["model"], effort=rq["effort"], context_hash=rq.get("context_hash", ""),
        system_hash=system_hash, user_hash=user_hash, schema_hash=schema_hash,
    )


#: fields the old key lacked, oldest layout first: records made before staged files existed were keyed
#: without `stage_files`, under the same record version
OLD_LAYOUTS: tuple[tuple[str, ...], ...] = ((), ("stage_files",))


def v1_key(fields: dict[str, Any], without: tuple[str, ...] = ()) -> str:
    """An old key: the same dictionary without the three text hashes (and, for the oldest records, without
    the fields the key did not yet have)."""
    return sha256_json({k: v for k, v in fields.items() if k not in HASH_KEYS and k not in without})


def family_of(rec: dict[str, Any]) -> str | None:
    """Which backend family recorded this call: the one whose old key, under some old layout, recomputes to
    the record's own. None means the record does not describe the call its key names."""
    said = rec.get("response", {}).get("backend")
    for family in ((said,) if said else ()) + FAMILIES:
        fields = key_fields_from_record(rec, family)
        if any(v1_key(fields, without) == rec.get("cache_key") for without in OLD_LAYOUTS):
            return family
    return None


# ---------------------------------------------------------------- the rebuilt prompt


def _classes(row: dict[str, Any], idx: dict[str, Any]) -> list[str]:
    """The routed class list the scheduler put in the prompt, from the class the page was read with and the
    index's candidates (see `Scheduler.request_for`)."""
    cands = [c.strip() for c in str(idx.get("route_candidates") or "").split(",") if c.strip()]
    cls = row.get("route_class") or "other"
    classes = [cls, *(c for c in cands if c != cls)]
    return [c for c in classes if c not in ("other", "uncertain")] or cands


def rebuild_user_prompt(rec: dict[str, Any], ctx: Context) -> tuple[str | None, str]:
    """The user prompt an extract_page record was made with, or None and the reason it cannot be known."""
    from ..extract import carry_from_result, context_hash

    row = ctx.by_key.get(rec["cache_key"])
    if row is None:
        return None, "no extract results row names this call"
    idx = ctx.index.get(row.get("page_id", ""))
    if idx is None:
        return None, f"page {row.get('page_id')} is not in the page index"
    carry = []
    if int(row.get("chain_pos") or 1) > 1:
        prev = ctx.by_page.get((row["pdf_sha256"], int(row["page_no"]) - 1))
        if prev is not None and prev.get("result"):
            carry = carry_from_result(prev["result"], int(prev["page_no"]))
    if context_hash(carry) != rec["request"].get("context_hash", ""):
        return None, "the carried context cannot be rebuilt to the record's context_hash"
    return user_prompt(_classes(row, idx), carry), ""


# ---------------------------------------------------------------- the walk


def rekey(root: Path | None = None, log: Callable[[str], None] = print, dry_run: bool = False) -> dict[str, int]:
    """Walk every record under the cache and write each movable v1 extract record under its new key.

    Returns counts: `rekeyed`, `skipped_other_task`, `skipped_version_mismatch`, `already_v2` (records already
    in the new layout), `already_rekeyed` (v1 records whose new-key twin exists), `unrebuildable` (v1 extract
    records whose prompt could not be rebuilt) and `files` (everything walked)."""
    counts = {"files": 0, "rekeyed": 0, "skipped_other_task": 0, "skipped_version_mismatch": 0,
              "already_v2": 0, "already_rekeyed": 0, "unrebuildable": 0}
    ctx: Context | None = None
    for path in sorted(calls_dir(root).rglob("*.json")):
        try:
            rec = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        counts["files"] += 1
        if rec.get("version") != V1:
            counts["already_v2"] += 1
            continue
        rq = rec.get("request") or {}
        if rq.get("task") != "extract_page":
            counts["skipped_other_task"] += 1
            continue
        if ctx is None:
            ctx = Context.load()
        if rq.get("prompt_version") != ctx.prompt_version or rq.get("schema_version") != ctx.schema_version:
            counts["skipped_version_mismatch"] += 1
            continue
        family = family_of(rec)
        if family is None:
            counts["unrebuildable"] += 1
            log(f"  {path.name}: its own key does not recompute from the record; left alone")
            continue
        prompt, why = rebuild_user_prompt(rec, ctx)
        if prompt is None:
            counts["unrebuildable"] += 1
            log(f"  {path.name}: {why}; left alone")
            continue
        fields = key_fields_from_record(rec, family, ctx.system_hash, hash_text(prompt), ctx.schema_hash)
        new_key = sha256_json(fields)
        target = record_path(new_key, root)
        if target.is_file():
            counts["already_rekeyed"] += 1
            continue
        moved = {
            **rec, "version": RECORD_VERSION, "cache_key": new_key,
            "request": {**rq, "system_hash": fields["system_hash"], "user_hash": fields["user_hash"],
                        "schema_hash": fields["schema_hash"]},
            "rekeyed_from": rec["cache_key"],
            "rekeyed_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        }
        if not dry_run:
            write_atomic(target, json.dumps(moved, separators=(",", ":")) + "\n")
        counts["rekeyed"] += 1
    log("  " + ", ".join(f"{k} {v}" for k, v in counts.items()) + (" (dry run, nothing written)" if dry_run else ""))
    return counts


def stats(root: Path | None = None) -> dict[tuple[str, str], int]:
    """Records by (task, record version), for `lr cache stats`."""
    out: dict[tuple[str, str], int] = {}
    for path in calls_dir(root).rglob("*.json"):
        try:
            rec = json.loads(path.read_text())
        except (json.JSONDecodeError, OSError):
            key = ("unreadable", "?")
        else:
            key = (str((rec.get("request") or {}).get("task") or "?"), str(rec.get("version") or "?"))
        out[key] = out.get(key, 0) + 1
    return out
