"""Build, audit and show an interface benchmark version: `knowledge/bench/interface/<version>/`, hashed.

The layout is the contract:

    tier1.jsonl      one deterministic item per line, gold as value ids and values, or abstention with a reason
    tier3.jsonl      one adversarial item per line, its source failure and reference, gold as the expected behaviour
    manifest.json    spec, seed, the analyst benchmark drawn from (version, manifest hash, cells hash, where
                     the out-of-fold scores came from), store hash, git commit, tool version, the session
                     rules applied, counts by tier, kind, stratum, reason, source and behaviour, shortfalls,
                     sha256 of each tier file, and the content hash over both

The build is deterministic: the same store snapshot, the same analyst benchmark and the same seed give the
same content hash, because every draw comes from one seeded generator consumed in a fixed order and every
number comes from a deterministic tool. Items are numbered after a stable sort, so the order on disk does
not depend on the order they were made in. The audit trusts none of it: it re-hashes the files, recomputes
the content hash, regenerates every item from the choice it recorded and compares, and checks each
unanswerable item is still unanswerable and each adversarial item still names a source the tier admits.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import random
from collections import Counter
from pathlib import Path
from typing import Any, Callable

from ...ids import sha256_file
from ...paths import PATHS
from ...prospect import tools as T
from ...store import snapshot as SN
from ..build import bench_dir as analyst_data_dir
from ..spec import STRATA
from . import tier1, tier3
from .items import BANNED, GRID, NONE, REASONS, Ctx, check_wording
from .reader import Reader, StoreReader, frozen_oof
from .spec import InterfaceSpec, load_spec

MANIFEST_VERSION = "bench-interface-manifest/v1"
TIER_FILES = ("tier1.jsonl", "tier3.jsonl")
#: the analyst session's rules the gold is computed under (reader.py says how)
SESSION_RULES = ("B17", "B18", "B30")


def interface_root() -> Path:
    return PATHS.pipeline / "knowledge" / "bench" / "interface"


def interface_dir(version: str, root: Path | None = None) -> Path:
    return (root or interface_root()) / version


def analyst_dir(version: str) -> Path:
    """The analyst benchmark's directory: the committed copy under `knowledge/bench/` when it holds the cell
    list, else the built one under `data/bench/`."""
    committed = PATHS.pipeline / "knowledge" / "bench" / version
    if (committed / "cells.jsonl").is_file():
        return committed
    built = analyst_data_dir(version)
    if (built / "cells.jsonl").is_file():
        return built
    raise FileNotFoundError(f"no analyst benchmark {version!r}: neither {committed} nor {built} holds cells.jsonl")


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _line(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True, ensure_ascii=False) + "\n"


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        return []
    return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


def content_sha256(out: Path) -> str:
    """One hash over the tier files, in a fixed order: what "the same benchmark" means."""
    h = hashlib.sha256()
    for name in TIER_FILES:
        p = out / name
        h.update(name.encode())
        h.update(p.read_bytes() if p.is_file() else b"")
    return h.hexdigest()


# ---------------------------------------------------------------- the analyst benchmark, as drawn on


def load_analyst(version: str, split: str, adir: Path | None = None) -> dict[str, Any]:
    """The cells this benchmark may draw on, their blind-lists and out-of-fold scores, and the hashes that
    name the analyst benchmark they came from."""
    d = adir or analyst_dir(version)
    cells = _read_jsonl(d / "cells.jsonl")
    drawable = [c for c in cells if split == "all" or c.get("split") == "open"]
    blind: dict[str, list[str]] = {}
    for c in cells:
        p = d / "blind" / f"{c['bench_id']}.json"
        if p.is_file():
            raw = json.loads(p.read_text())
            blind[str(c["bench_id"])] = [str(f) for f in (raw.get("files") if isinstance(raw, dict) else raw) or []]
    oof_csv = d / "oof_scores.csv"
    oof = frozen_oof(oof_csv, cells) if oof_csv.is_file() else None
    manifest = d / "manifest.json"
    return {
        "version": version, "dir": d, "cells": cells, "drawable": drawable, "blind": blind, "oof": oof,
        "record": {
            "version": version, "split": split,
            "dir": d.relative_to(PATHS.root).as_posix() if d.is_relative_to(PATHS.root) else str(d),
            "manifest_sha256": sha256_file(manifest) if manifest.is_file() else None,
            "cells_sha256": sha256_file(d / "cells.jsonl"),
            "oof_source": "oof_scores.csv" if oof is not None else "derived.cell_score_oof",
            "cells_drawable": len(drawable), "blind_lists": len(blind),
        },
    }


def make_ctx(spec: InterfaceSpec, analyst: dict[str, Any], reader: Reader | None = None) -> Ctx:
    reader = reader or StoreReader(analyst["cells"], oof=analyst["oof"], blind=analyst["blind"])
    return Ctx(reader=reader, cells=list(analyst["drawable"]), nearby_radius_m=spec.tier1.nearby_radius_m,
               label_radius_km=spec.tier1.label_radius_km, nearby_layers=tuple(spec.tier1.nearby_layers))


# ---------------------------------------------------------------- build


def _order(item: dict[str, Any], names: list[str]) -> tuple:
    kind = item["kind"]
    strata = (*STRATA, GRID, NONE)
    return (names.index(kind) if kind in names else len(names), strata.index(item["stratum"]) if item["stratum"] in strata else 99,
            str(item.get("bench_id") or ""), json.dumps(item.get("choice"), sort_keys=True), item["question"])


def _number(items: list[dict[str, Any]], names: list[str], prefix: str) -> list[dict[str, Any]]:
    items = sorted(items, key=lambda it: _order(it, names))
    for i, it in enumerate(items):
        it["id"] = f"{prefix}-{i + 1:04d}"
    return items


def build(version: str, log: Callable[[str], None] = print, root: Path | None = None,
          reader: Reader | None = None, adir: Path | None = None) -> dict[str, Any]:
    """Write `knowledge/bench/interface/<version>/` from the spec, the analyst benchmark and the store, and
    return the manifest. Read-only on the store. `reader` and `adir` exist for tests on fixtures."""
    spec = load_spec(version)
    analyst = load_analyst(spec.analyst_version, spec.split, adir)
    ctx = make_ctx(spec, analyst, reader)
    log(f"  analyst benchmark {spec.analyst_version}: {len(analyst['drawable'])} drawable cells of "
        f"{len(analyst['cells'])} ({spec.split} split), out-of-fold scores from {analyst['record']['oof_source']}")
    rng = random.Random(spec.seed)
    t1, short1 = tier1.generate(ctx, spec.tier1.kinds, rng)
    log(f"  tier 1: {len(t1)} items" + (f"; short {short1}" if short1 else ""))
    t3, short3 = tier3.generate(ctx, spec.tier3.sources, rng)
    log(f"  tier 3: {len(t3)} items" + (f"; short {short3}" if short3 else ""))
    t1 = _number(t1, list(spec.tier1.kinds), "t1")
    t3 = _number(t3, list(spec.tier3.sources), "t3")

    out = interface_dir(version, root)
    out.mkdir(parents=True, exist_ok=True)
    (out / "tier1.jsonl").write_text("".join(_line(it) for it in t1))
    (out / "tier3.jsonl").write_text("".join(_line(it) for it in t3))
    manifest = {
        "manifest_version": MANIFEST_VERSION, "version": version, "seed": spec.seed,
        "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "spec": spec.as_dict(), "analyst": analyst["record"],
        "store_sha256": SN.store_sha(), "git_commit": SN._git_commit(), "tool_version": T.TOOL_VERSION,
        "session_rules": list(SESSION_RULES),
        "switches": {"label_context": True, "oof_scores": True, "effort_features": True},
        "counts": counts(t1, t3),
        "shortfalls": {"tier1": short1, "tier3": short3},
        "files": {name: sha256_file(out / name) for name in TIER_FILES},
        "content_sha256": content_sha256(out),
        "status": "built, not run: no agent has been scored on these tiers",
    }
    (out / "manifest.json").write_text(_dump(manifest))
    log(f"  manifest: content {manifest['content_sha256'][:12]}, store {str(manifest['store_sha256'])[:12]}")
    return manifest


def counts(t1: list[dict[str, Any]], t3: list[dict[str, Any]]) -> dict[str, Any]:
    reasons = Counter(it["gold"]["reason"] for it in t1 if it["gold"]["answer"] == "abstain")
    return {
        "tier1": {
            "items": len(t1),
            "answerable": sum(1 for it in t1 if it["gold"]["answer"] != "abstain"),
            "unanswerable": sum(1 for it in t1 if it["gold"]["answer"] == "abstain"),
            "kinds": dict(sorted(Counter(it["kind"] for it in t1).items())),
            "strata": dict(sorted(Counter(it["stratum"] for it in t1).items())),
            "reasons": {r: int(reasons.get(r, 0)) for r in REASONS},
        },
        "tier3": {
            "items": len(t3),
            "sources": dict(sorted(Counter(it["kind"] for it in t3).items())),
            "families": dict(sorted(Counter(it.get("family") for it in t3).items())),
            "behaviours": dict(sorted(Counter(it["gold"]["behaviour"] for it in t3).items())),
            "strata": dict(sorted(Counter(it["stratum"] for it in t3).items())),
        },
    }


# ---------------------------------------------------------------- audit


def _same(a: dict[str, Any], b: dict[str, Any]) -> list[str]:
    """Which fields of a written item differ from its regeneration; the id is the numbering's, not the maker's."""
    skip = {"id"}
    diffs = []
    for k in sorted(set(a) | set(b)):
        if k in skip:
            continue
        if json.loads(json.dumps(a.get(k), sort_keys=True)) != json.loads(json.dumps(b.get(k), sort_keys=True)):
            diffs.append(k)
    return diffs


def audit(version: str, root: Path | None = None, reader: Reader | None = None, adir: Path | None = None,
          limit: int | None = None) -> list[str]:
    """Problems with a built interface benchmark; empty means it stands.

    Re-hashes the tier files and the content hash against the manifest, checks the spec on disk is the one
    the manifest was built from, then regenerates every item (or the first `limit` per tier) from its
    recorded choice through the same reader and compares: every gold id resolves in the store with the same
    value, every unanswerable item is still unanswerable (the feature still unmeasured, the cell still not
    in the grid, the quantity still absent), and every adversarial item still names an admitted source, its
    reference and a corrupted token that is not the truth. Wording is checked again on the way."""
    out = interface_dir(version, root)
    problems: list[str] = []
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        return [f"no manifest at {manifest_path}"]
    manifest = json.loads(manifest_path.read_text())
    for name, sha in (manifest.get("files") or {}).items():
        p = out / name
        if not p.is_file():
            problems.append(f"{name}: missing")
        elif sha256_file(p) != sha:
            problems.append(f"{name}: hash differs from the manifest")
    if content_sha256(out) != manifest.get("content_sha256"):
        problems.append("content hash differs from the manifest")
    try:
        spec = load_spec(version)
    except Exception as err:  # noqa: BLE001  (the reason is the message)
        return problems + [f"spec: {err}"]
    if json.loads(json.dumps(spec.as_dict())) != manifest.get("spec"):
        problems.append("the spec on disk is not the one the manifest was built from")
    if manifest.get("tool_version") != T.TOOL_VERSION:
        problems.append(f"tool version {manifest.get('tool_version')} in the manifest, {T.TOOL_VERSION} in the code")

    analyst = load_analyst(spec.analyst_version, spec.split, adir)
    if analyst["record"]["cells_sha256"] != (manifest.get("analyst") or {}).get("cells_sha256"):
        problems.append("the analyst benchmark's cell list is not the one the manifest names")
    ctx = make_ctx(spec, analyst, reader)
    by_bench = {str(c["bench_id"]): c for c in analyst["cells"]}
    drawable = {str(c["bench_id"]) for c in analyst["drawable"]}

    t1 = _read_jsonl(out / "tier1.jsonl")
    t3 = _read_jsonl(out / "tier3.jsonl")
    if not t1 and not t3:
        problems.append("no items on disk")
    seen_ids: set[str] = set()
    for tier, items, regen in ((1, t1, tier1.regenerate), (3, t3, tier3.regenerate)):
        for item in items[:limit] if limit else items:
            iid = str(item.get("id"))
            if iid in seen_ids:
                problems.append(f"{iid}: duplicate id")
            seen_ids.add(iid)
            problems += [f"{iid}: {why}" for why in _check_item(tier, item, by_bench, drawable)]
            try:
                again = regen(ctx, item)
            except Exception as err:  # noqa: BLE001
                problems.append(f"{iid}: regeneration failed: {err}")
                continue
            if again is None:
                problems.append(f"{iid}: the store no longer yields this item from its recorded choice")
                continue
            diffs = _same(item, again)
            if diffs:
                problems.append(f"{iid}: differs from its regeneration in {', '.join(diffs)}")
    return problems


def _check_item(tier: int, item: dict[str, Any], by_bench: dict[str, dict[str, Any]], drawable: set[str]) -> list[str]:
    """The checks that do not need the store: shape, vocabulary, provenance, wording."""
    why: list[str] = []
    gold = item.get("gold") or {}
    try:
        check_wording(item.get("question"), item.get("premise"), gold.get("note"))
    except Exception as err:  # noqa: BLE001
        why.append(str(err))
    if item.get("tier") != tier:
        why.append(f"tier {item.get('tier')} in a tier {tier} file")
    bid = item.get("bench_id")
    if item["kind"] == "outside_grid":
        if bid in by_bench:
            why.append(f"outside-grid item names a benchmark cell {bid}")
        if gold.get("reason") != "outside_grid" or gold.get("answer") != "abstain":
            why.append("outside-grid gold is not abstention with reason outside_grid")
    elif item.get("stratum") == GRID:
        if bid is not None or item.get("cell_id") is not None:
            why.append("grid-level item names a cell")
    else:
        cell = by_bench.get(str(bid))
        if cell is None:
            why.append(f"bench id {bid} is not in the analyst benchmark")
        else:
            if str(bid) not in drawable:
                why.append(f"bench id {bid} is outside the drawable split")
            if cell.get("cell_id") != item.get("cell_id") or cell.get("stratum") != item.get("stratum") \
                    or (cell.get("fold") is not None and int(cell["fold"]) != item.get("fold")):
                why.append("cell id, stratum or fold disagree with the analyst benchmark's cell list")
    if gold.get("answer") == "abstain" and gold.get("reason") not in REASONS:
        why.append(f"abstention reason {gold.get('reason')!r} is not in {REASONS}")
    if gold.get("answer") != "abstain" and tier == 1 and not gold.get("value_ids") and gold.get("keys") is None:
        why.append("an answerable item with no value id and no key set")
    if tier == 3:
        src = tier3.SOURCES.get(str(item.get("source")))
        if src is None or item.get("kind") != item.get("source"):
            why.append(f"source {item.get('source')!r} is not one the tier admits")
        elif item.get("reference") != src.reference or item.get("family") != src.family:
            why.append("reference or family differs from the source's record")
        if gold.get("behaviour") not in tier3.BEHAVIOURS:
            why.append(f"behaviour {gold.get('behaviour')!r} is not in {tier3.BEHAVIOURS}")
    return why


# ---------------------------------------------------------------- show


def show(version: str, root: Path | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Counts by tier, kind and stratum, the shortfalls and the hashes, from the manifest."""
    out = interface_dir(version, root)
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        log(f"  no interface benchmark {version} at {out}; run `ue bench interface build --version {version}`")
        return {}
    m = json.loads(manifest_path.read_text())
    a = m.get("analyst") or {}
    log(f"  UraniumBench interface {m['version']}  seed {m['seed']}  built {m.get('built_at')}  {m.get('status', '')}")
    log(f"  content {str(m.get('content_sha256'))[:12]}  store {str(m.get('store_sha256'))[:12]}  "
        f"commit {str(m.get('git_commit'))[:12]}  tools {m.get('tool_version')}")
    log(f"  analyst benchmark {a.get('version')} ({a.get('split')} split, {a.get('cells_drawable')} cells, "
        f"manifest {str(a.get('manifest_sha256'))[:12]}, scores from {a.get('oof_source')}); "
        f"session rules {', '.join(m.get('session_rules') or [])}")
    c = m.get("counts") or {}
    t1, t3 = c.get("tier1") or {}, c.get("tier3") or {}
    log(f"  tier 1: {t1.get('items', 0)} items, {t1.get('answerable', 0)} answerable, {t1.get('unanswerable', 0)} unanswerable")
    log("    kinds: " + ", ".join(f"{k} {n}" for k, n in (t1.get("kinds") or {}).items()))
    log("    strata: " + ", ".join(f"{k} {n}" for k, n in (t1.get("strata") or {}).items()))
    log("    reasons: " + ", ".join(f"{k} {n}" for k, n in (t1.get("reasons") or {}).items()))
    log(f"  tier 3: {t3.get('items', 0)} items")
    log("    sources: " + ", ".join(f"{k} {n}" for k, n in (t3.get("sources") or {}).items()))
    log("    behaviours: " + ", ".join(f"{k} {n}" for k, n in (t3.get("behaviours") or {}).items()))
    log("    strata: " + ", ".join(f"{k} {n}" for k, n in (t3.get("strata") or {}).items()))
    short = m.get("shortfalls") or {}
    for tier in ("tier1", "tier3"):
        if short.get(tier):
            log(f"  {tier} shortfalls: " + ", ".join(f"{k} {n}" for k, n in short[tier].items()))
    return m


def load_items(version: str, tier: int, root: Path | None = None) -> list[dict[str, Any]]:
    """The items of one tier of a built interface benchmark, for anything that runs or scores against it."""
    return _read_jsonl(interface_dir(version, root) / f"tier{tier}.jsonl")


__all__ = ["BANNED", "audit", "build", "content_sha256", "interface_dir", "load_analyst", "load_items", "show"]
