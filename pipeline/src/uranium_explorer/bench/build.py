"""Build, audit and show a benchmark version: `data/bench/<version>/`, hashed, from the spec and the store.

The layout is the contract:

    cells.jsonl              bench_id, cell_id, stratum, fold, split   (the builder's record; not for models)
    packs/<bench_id>.json    the anonymised evidence pack
    cards/<bench_id>.png     the map card
    blind/<bench_id>.json    the files retrieval must not return for this cell
    passages/<bench_id>.json scrubbed retrieved passages, only where there are any
    oof_scores.csv           bench_id, model, fold_kind, fold, score for the benchmark cells
    key.json                 the answer key, hashed on its own and never inside a pack
    heldout.json             the held-out bench ids
    manifest.json            spec, seed, store hash, git commit, counts, shortfalls, sha256 of every file

The build is resumable: a file whose hash still matches the previous manifest is not rewritten, so a run that
stopped halfway through the cards picks up where it was. Every random draw comes from the spec's seed, so a
resumed build and a fresh one agree byte for byte. The audit trusts none of that and checks: it re-hashes,
it scans every pack and passage for anything that places or names the ground, and it checks the held-out ids
against the cell list.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Callable

import duckdb
import pandas as pd
from shapely import wkb

from ..paths import PATHS
from ..prospect import tools as T
from ..prospect.extended import EXTENDED_FEATURES
from ..store import connect
from ..store import snapshot as SN
from .blind import blind_list, passages
from .card import card_layers, png_bytes, render_card
from .dataset import hint
from .frame import load_frame
from .oof import COLUMNS as OOF_COLUMNS
from .oof import oof_scores
from .pack import FORBIDDEN_KEYS, NORTHING, PATTERNS, name_pattern, build_pack, forbidden_strings, KEEP_TEXT
from .sample import assign_bench_ids, sample_cells, shortfalls
from .spec import LATER_PACK_SWITCHES, STRATA, load_spec, spec_path

MANIFEST_VERSION = "bench-manifest/v1"
SUBDIRS = ("packs", "cards", "cards_drillholes", "blind", "passages")
#: a benchmark with cell-level text also keeps its raw view and its sources, for the audit only
TEXT_SUBDIRS = ("passages_raw", "sources")
#: keys an answer key has and a pack must not
KEY_FIELDS = ("label", "stratum", "split", "heldout")


def bench_dir(version: str, root: Path | None = None) -> Path:
    return (root or PATHS.data / "bench") / version


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        while block := fh.read(1 << 20):
            h.update(block)
    return h.hexdigest()


def _dump(obj: Any) -> str:
    return json.dumps(obj, indent=1, sort_keys=True, ensure_ascii=False) + "\n"


def _fresh(path: Path, previous: dict[str, str], rel: str) -> bool:
    """True when the file is on disk with the hash the last manifest recorded for it."""
    return path.is_file() and previous.get(rel) == sha256_file(path)


def _previous_cells(out: Path, previous: dict[str, str]) -> dict[str, str]:
    """The last build's bench id to cell map, when its cell list is the one its manifest hashed; empty otherwise.

    A file is fresh for a bench id only while that id names the same cell. The sample can move with the store
    while the spec and the seed stay the same, and a pack, card or passage kept across that move describes other
    ground under a key that labels this ground."""
    path = out / "cells.jsonl"
    if not previous or not path.is_file() or previous.get("cells.jsonl") != sha256_file(path):
        return {}
    rows = [json.loads(line) for line in path.read_text().splitlines() if line.strip()]
    return {str(r["bench_id"]): str(r["cell_id"]) for r in rows}


def _pack_switches(path: Path) -> dict[str, bool] | None:
    try:
        return dict(json.loads(path.read_text()).get("switches") or {})
    except (OSError, json.JSONDecodeError):
        return None


def _write_if_changed(path: Path, data: bytes) -> bool:
    if path.is_file() and path.read_bytes() == data:
        return False
    path.write_bytes(data)
    return True


# ---------------------------------------------------------------- build


def build(version: str, log: Callable[[str], None] = print, root: Path | None = None,
          fit: Callable[..., Any] | None = None, con: Any = None) -> dict[str, Any]:
    """Write `data/bench/<version>/` from the spec and the store, and return the manifest.

    Read-only on the store: the out-of-fold scores go to `oof_scores.csv` here, and into the store only
    through `ue bench oof-scores --write`. `fit` and `con` exist for tests on synthetic stores."""
    spec = load_spec(version)
    out = bench_dir(version, root)
    for sub in SUBDIRS + (TEXT_SUBDIRS if spec.text else ()):
        (out / sub).mkdir(parents=True, exist_ok=True)
    # the pre-registration, when there is one, is part of the benchmark: hashed with it, so a result can only
    # report what was fixed before the first call
    prereg = spec_path(f"prereg-{version}")
    if prereg.is_file():
        _write_if_changed(out / "prereg.toml", prereg.read_bytes())
    manifest_path = out / "manifest.json"
    previous: dict[str, str] = {}
    if manifest_path.is_file():
        last = json.loads(manifest_path.read_text())
        same_spec = json.loads(json.dumps(last.get("spec"))) == json.loads(json.dumps(spec.as_dict(), default=list))
        if same_spec and last.get("seed") == spec.seed:
            previous = last.get("files", {})
            log(f"  resuming: {len(previous)} file(s) in the previous manifest")
        else:
            # a changed spec is a different benchmark: nothing from the last build is fresh, whatever its hash
            log("  spec changed since the last build: rebuilding every file")

    own = con is None
    con = con or connect(read_only=True)
    try:
        extended = spec.pack.extended_features
        df = load_frame(con=con, extra=EXTENDED_FEATURES if extended else ())
        log(f"  frame: {len(df):,} scorable cells, {(df['label_tier'] != 'unlabelled').sum()} positives")
        cells = assign_bench_ids(sample_cells(df, spec), spec.seed)
        short = shortfalls(cells, spec)
        counts = {s: int((cells["stratum"] == s).sum()) for s in STRATA}
        log("  sampled: " + ", ".join(f"{s} {n}" for s, n in counts.items())
            + (f"; short {short}" if short else ""))

        oof = oof_scores(spec.seed, df=df, fold_km=spec.fold_km, n_folds=spec.n_folds, fit=fit, extended=extended)
        bench_oof = (cells[["bench_id", "cell_id"]].merge(oof, on="cell_id")
                     .drop(columns=["cell_id"]).sort_values(["bench_id", "model"]).reset_index(drop=True))
        bench_oof = bench_oof[["bench_id", *[c for c in OOF_COLUMNS if c != "cell_id"]]]
        (out / "oof_scores.csv").write_text(bench_oof.to_csv(index=False, lineterminator="\n"))
        log(f"  out-of-fold scores: {len(bench_oof):,} rows for the benchmark cells")

        forbidden = forbidden_strings(con)
        if spec.pack.later():
            # a benchmark built with the later switches is closed-book to the prompt's standard: the passages
            # are scrubbed of its place names as the packs are (`pack.build_pack`), and the audit checks both
            from ..analyst.v0 import PLACE_NAMES

            forbidden = forbidden | set(PLACE_NAMES)
        coverage = T.coverage()
        layers = card_layers(spec)
        log("  card layers: " + ", ".join(f"{k} {len(v):,}" for k, v in layers.items()))
        ids = list(cells["cell_id"])
        placeholders = ",".join("?" * len(ids))
        geoms = {cid: wkb.loads(bytes(g)) for cid, g in con.execute(
            f"select cell_id, geom_wkb from derived.cell where cell_id in ({placeholders})", ids).fetchall()}

        texts = None
        if spec.text:
            from .celltext import build_texts

            texts = build_texts([(str(r.bench_id), str(r.cell_id)) for r in cells.itertuples(index=False)],
                                spec.text, con, forbidden, log=log)
        written = {"packs": 0, "cards": 0, "cards_drillholes": 0, "blind": 0, "passages": 0}
        before = _previous_cells(out, previous)
        moved = sum(before.get(str(r.bench_id)) != str(r.cell_id) for r in cells.itertuples(index=False))
        if previous and moved:
            log(f"  {moved} bench id(s) now name a different cell than last build: their files are rebuilt")
        for row in cells.itertuples(index=False):
            bid, cid = str(row.bench_id), str(row.cell_id)
            # nothing is fresh for a bench id that names another cell than it did: its files describe that cell
            kept = previous if before.get(bid) == cid else {}
            pack_path = out / "packs" / f"{bid}.json"
            # a pack is fresh only if its hash matches the last manifest and it was built with the spec's
            # switches: the switches are written into the pack, so a stale pack under a new spec is caught
            if not (_fresh(pack_path, kept, f"packs/{bid}.json") and _pack_switches(pack_path) == spec.pack.switches()):
                pack = build_pack(cid, bid, spec, con=con, forbidden=forbidden, oof=oof, shared={"coverage": coverage})
                pack_path.write_text(_dump(pack))
                written["packs"] += 1
            card_path = out / "cards" / f"{bid}.png"
            if not _fresh(card_path, kept, f"cards/{bid}.png"):
                img = render_card(cid, spec, layers, geoms[cid], drillholes=spec.card.drillholes)
                card_path.write_bytes(png_bytes(img))
                written["cards"] += 1
            if spec.card.drillholes_variant:
                holes_path = out / "cards_drillholes" / f"{bid}.png"
                if not _fresh(holes_path, kept, f"cards_drillholes/{bid}.png"):
                    img = render_card(cid, spec, layers, geoms[cid], drillholes=True)
                    holes_path.write_bytes(png_bytes(img))
                    written["cards_drillholes"] += 1
            blind_path = out / "blind" / f"{bid}.json"
            files = blind_list(cid, spec.blind.radius_km, con)
            if _write_if_changed(blind_path, _dump({"bench_id": bid, "radius_km": spec.blind.radius_km,
                                                    "files": files}).encode()):
                written["blind"] += 1
            pass_path = out / "passages" / f"{bid}.json"
            if texts is not None:
                # cell-level report text, redacted: the view arms read; the raw view and the sources are for the
                # leak audit, and no arm is given either
                for sub, view in (("passages", "redacted"), ("passages_raw", "raw"), ("sources", "sources")):
                    path = out / sub / f"{bid}.json"
                    rows_ = texts[bid][view]
                    if rows_:
                        if _write_if_changed(path, _dump({"bench_id": bid, "passages": rows_}).encode()):
                            written["passages"] += sub == "passages"
                    elif path.is_file():
                        path.unlink()
            elif not _fresh(pass_path, kept, f"passages/{bid}.json"):
                found = passages(cid, spec, files, con, forbidden=forbidden)
                if found:
                    pass_path.write_text(_dump({"bench_id": bid, "passages": found}))
                    written["passages"] += 1
                elif pass_path.is_file():
                    pass_path.unlink()
        log("  wrote: " + ", ".join(f"{k} {n}" for k, n in written.items()))
    finally:
        if own:
            con.close()

    tiers = dict(zip(df["cell_id"], df["label_tier"], strict=True))
    (out / "cells.jsonl").write_text("".join(
        json.dumps({"bench_id": r.bench_id, "cell_id": r.cell_id, "stratum": r.stratum, "fold": int(r.fold),
                    "split": r.split}) + "\n" for r in cells.itertuples(index=False)))
    key = {r.bench_id: {"label": tiers[r.cell_id], "stratum": r.stratum, "fold": int(r.fold), "split": r.split}
           for r in cells.itertuples(index=False)}
    (out / "key.json").write_text(_dump(key))
    heldout = sorted(cells.loc[cells["split"] == "heldout", "bench_id"])
    (out / "heldout.json").write_text(_dump(heldout))

    manifest = {
        "manifest_version": MANIFEST_VERSION, "version": version, "seed": spec.seed,
        "built_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "spec": spec.as_dict(), "store_sha256": SN.store_sha(), "git_commit": SN._git_commit(),
        "counts": {"cells": int(len(cells)), "stratum": counts,
                   "split": {s: int((cells["split"] == s).sum()) for s in ("open", "heldout")}},
        "shortfalls": short,
        "key_sha256": sha256_file(out / "key.json"),
        "files": _hash_tree(out),
    }
    manifest_path.write_text(_dump(manifest))
    log(f"  manifest: {len(manifest['files'])} file(s), store {str(manifest['store_sha256'])[:12]}, "
        f"key {manifest['key_sha256'][:12]}")
    return manifest


def _hash_tree(out: Path) -> dict[str, str]:
    """Every benchmark file but the manifest and the key, hashed, keyed by its path under the version dir."""
    skip = {"manifest.json", "key.json"}
    files = {}
    for p in sorted(out.rglob("*")):
        if p.is_file() and p.name not in skip:
            files[p.relative_to(out).as_posix()] = sha256_file(p)
    return files


# ---------------------------------------------------------------- audit


def audit(version: str, root: Path | None = None, con: Any = None) -> list[str]:
    """Problems with a built benchmark; empty means it stands.

    Re-hashes every file against the manifest (and the key on its own), scans every pack and passage for a
    real cell id, a coordinate, an NTS sheet, a file number, a forbidden key or a company, property or deposit
    name from the store, and checks that held-out ids are marked held out in the cell list."""
    out = bench_dir(version, root)
    problems: list[str] = []
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        return [f"no benchmark {version} at {out}; " + hint(f"ue bench build --version {version}")]
    manifest = json.loads(manifest_path.read_text())
    files: dict[str, str] = manifest.get("files", {})
    for rel, sha in files.items():
        p = out / rel
        if not p.is_file():
            problems.append(f"{rel}: missing")
        elif sha256_file(p) != sha:
            problems.append(f"{rel}: hash differs from the manifest")
    for rel in sorted(set(_hash_tree(out)) - set(files)):
        problems.append(f"{rel}: on disk but not in the manifest")
    key_path = out / "key.json"
    if not key_path.is_file():
        problems.append("key.json: missing")
    elif sha256_file(key_path) != manifest.get("key_sha256"):
        problems.append("key.json: hash differs from the manifest")

    cells_path = out / "cells.jsonl"
    rows = [json.loads(line) for line in cells_path.read_text().splitlines() if line.strip()] if cells_path.is_file() else []
    if not rows:
        problems.append("cells.jsonl: missing or empty")
    cell_ids = {r["cell_id"] for r in rows}
    split = {r["bench_id"]: r["split"] for r in rows}
    heldout = json.loads((out / "heldout.json").read_text()) if (out / "heldout.json").is_file() else []
    for bid in heldout:
        if split.get(bid) != "heldout":
            problems.append(f"{bid}: in heldout.json but marked {split.get(bid, 'absent')} in cells.jsonl")
    for bid, s in split.items():
        if s == "heldout" and bid not in set(heldout):
            problems.append(f"{bid}: marked heldout in cells.jsonl but not in heldout.json")

    names = _store_names(con)
    if names is None:
        problems.append("no store to read company, property and deposit names from; the name check did not run")
        names = set()
    if any((manifest.get("spec") or {}).get("pack", {}).get(k) for k in LATER_PACK_SWITCHES):
        # a pack built with the later switches is scrubbed of place names too, so the audit looks for them
        from ..analyst.v0 import PLACE_NAMES

        names = set(names) | set(PLACE_NAMES)
    name_pat = name_pattern(frozenset(names))
    has_text = bool((manifest.get("spec") or {}).get("text"))
    for sub in ("packs", "passages", *(("passages_raw",) if has_text else ())):
        for p in sorted((out / sub).glob("*.json")):
            try:
                payload = json.loads(p.read_text())
            except json.JSONDecodeError as err:
                problems.append(f"{sub}/{p.name}: not JSON ({err})")
                continue
            rel = f"{sub}/{p.name}"
            problems += [f"{rel}: {why}" for why in leaks(payload, cell_ids, name_pat, sub == "packs")]
            if has_text and sub == "passages":
                problems += [f"{rel}: {why}" for why in outcome_leaks(payload)]
    return problems


def outcome_leaks(payload: dict[str, Any]) -> list[str]:
    """Pure: in the redacted view, any passage that still states an outcome or carries a digit."""
    from .celltext import OUTCOME

    found = []
    for r in payload.get("passages") or []:
        text = str(r.get("text") or "")
        m = OUTCOME.search(text)
        if m:
            found.append(f"{r.get('passage_id')}: outcome word {m.group(0)!r}")
        if re.search(r"\d", text):
            found.append(f"{r.get('passage_id')}: a digit survived redaction")
    return found


def _store_names(con: Any) -> set[str] | None:
    own = con is None
    try:
        con = con or connect(read_only=True)
    except duckdb.Error:
        return None
    try:
        return forbidden_strings(con)
    except duckdb.Error:
        return None
    finally:
        if own:
            con.close()


def leaks(payload: Any, cell_ids: set[str], name_pat: Any, is_pack: bool) -> list[str]:
    """Pure: every way a pack or passage payload gives the ground away, as short reasons."""
    found: list[str] = []
    seen: set[str] = set()

    def note(why: str) -> None:
        if why not in seen:
            seen.add(why)
            found.append(why)

    def walk(node: Any, path: str, key: str | None = None) -> None:
        if isinstance(node, dict):
            for k, v in node.items():
                if k in FORBIDDEN_KEYS:
                    note(f"forbidden key {k!r} at {path or '/'}")
                if is_pack and k in KEY_FIELDS:
                    note(f"answer-key field {k!r} at {path or '/'}")
                walk(v, f"{path}/{k}", k)
        elif isinstance(node, list):
            for i, v in enumerate(node):
                walk(v, f"{path}[{i}]", key)
        elif isinstance(node, str):
            for cid in cell_ids:
                if cid in node:
                    note(f"cell id {cid} at {path}")
                    break
            for kind, pat in PATTERNS.items():
                m = pat.search(node)
                if m:
                    note(f"{kind} pattern {m.group(0)!r} at {path}")
            # the criteria table's evidence and caveat text is public literature, identical for every cell, and
            # names deposits on purpose; the scrubber keeps it, so the audit does not read it as a leak
            if name_pat is not None and key not in KEEP_TEXT:
                m = name_pat.search(node)
                if m:
                    note(f"name {m.group(0)!r} at {path}")
        elif isinstance(node, (int, float)) and not isinstance(node, bool):
            if NORTHING[0] <= float(node) <= NORTHING[1]:
                note(f"number {node} in the northing range at {path}")

    walk(payload, "")
    return found


# ---------------------------------------------------------------- show


def show(version: str, root: Path | None = None, log: Callable[[str], None] = print) -> dict[str, Any]:
    """Print what a built benchmark holds, from its manifest."""
    out = bench_dir(version, root)
    manifest_path = out / "manifest.json"
    if not manifest_path.is_file():
        log(f"  no benchmark {version} at {out}; " + hint(f"ue bench build --version {version}"))
        return {}
    m = json.loads(manifest_path.read_text())
    counts = m.get("counts", {})
    log(f"  UraniumBench {m['version']}  seed {m['seed']}  built {m.get('built_at')}")
    log(f"  store {str(m.get('store_sha256'))[:12]}  commit {str(m.get('git_commit'))[:12]}  "
        f"key {str(m.get('key_sha256'))[:12]}")
    log(f"  cells {counts.get('cells', 0)}: " + ", ".join(f"{s} {n}" for s, n in counts.get("stratum", {}).items()))
    log("  split: " + ", ".join(f"{s} {n}" for s, n in counts.get("split", {}).items()))
    if m.get("shortfalls"):
        log("  shortfalls: " + ", ".join(f"{s} {n}" for s, n in m["shortfalls"].items()))
    files = m.get("files", {})
    by_kind = {sub: sum(1 for f in files if f.startswith(sub + "/")) for sub in SUBDIRS}
    log("  files: " + ", ".join(f"{k} {n}" for k, n in by_kind.items()) + f", {len(files)} hashed in all")
    return m


def load_cells(version: str, root: Path | None = None) -> pd.DataFrame:
    """The cell list of a built benchmark, for anything that scores against it."""
    p = bench_dir(version, root) / "cells.jsonl"
    return pd.DataFrame([json.loads(line) for line in p.read_text().splitlines() if line.strip()])


__all__ = ["audit", "bench_dir", "build", "leaks", "load_cells", "show"]
