"""The frozen benchmark as the harness reads it: cells, packs, cards, passages, and the key kept apart.

A benchmark version under `data/bench/<version>/` is immutable once its manifest is written, and the answer
key sits in the same directory as the evidence. The two facts together are why this module exists: everything
the model is shown goes through `Bench.pack`, `Bench.card` and `Bench.passages`, and `key.json` is read only by
the scorer. Nothing here can stage the key by accident, because nothing here returns its path.

Where the build is absent, `bench_dir` falls back to the dataset copy under the knowledge directory
(`bench/dataset.py`: `pipeline/knowledge/bench/`, or `UE_BENCH_KNOWLEDGE_DIR`), which holds the cells, the
held-out list, the key and the manifest: enough to score a run against, not to run one, since the packs and
cards are built, never copied.

Held-out cells are never run before the day they are unsealed. `open_cells` excludes them and `require_open`
refuses a request that names one, so the only way to score a held-out cell is to change this module.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from functools import cached_property
from pathlib import Path
from typing import Any

from ..bench.dataset import hint, knowledge_dir
from .arms import LATER_SWITCHES
from ..ids import sha256_file
from ..paths import PATHS

STRATA = ("deposit", "occurrence", "negative", "probe")
SPLITS = ("open", "heldout")


def bench_root() -> Path:
    return PATHS.data / "bench"


def bench_dir(version: str) -> Path:
    """The built benchmark when its manifest is there, else the dataset copy when it holds one, else the
    built path, so a message can say where the build would go."""
    built = bench_root() / version
    if (built / "manifest.json").is_file():
        return built
    copy = knowledge_dir() / version
    if (copy / "manifest.json").is_file():
        return copy
    return built


@dataclass(frozen=True)
class Bench:
    version: str
    dir: Path
    manifest: dict[str, Any]
    manifest_sha256: str
    cells: tuple[dict[str, Any], ...]
    key: dict[str, dict[str, Any]]
    heldout: frozenset[str]

    def cell(self, bench_id: str) -> dict[str, Any]:
        for c in self.cells:
            if c["bench_id"] == bench_id:
                return c
        raise KeyError(f"no cell {bench_id!r} in benchmark {self.version}")

    def open_cells(self) -> list[dict[str, Any]]:
        return [c for c in self.cells if c.get("split") == "open" and c["bench_id"] not in self.heldout]

    def require_open(self, bench_ids: list[str]) -> list[dict[str, Any]]:
        """The named cells, or a refusal if any is held out or unknown. Order is the caller's."""
        sealed = [b for b in bench_ids if b in self.heldout]
        if sealed:
            raise PermissionError(f"held-out cells are never run before they are unsealed: {sealed}")
        return [self.cell(b) for b in bench_ids]

    # ---------------------------------------------------------------- what the model may see

    def pack(self, bench_id: str) -> dict[str, Any]:
        return json.loads((self.dir / "packs" / f"{bench_id}.json").read_text())

    def card(self, bench_id: str, drillholes: bool = False) -> Path | None:
        """The plain card, or the drillholes variant when the arm asks for holes and the build carries one
        (or the plain cards already draw them)."""
        if drillholes:
            variant = self.dir / "cards_drillholes" / f"{bench_id}.png"
            if variant.is_file():
                return variant
        p = self.dir / "cards" / f"{bench_id}.png"
        return p if p.is_file() else None

    def passages(self, bench_id: str, view: str = "own") -> list[dict[str, Any]]:
        """The builder writes `{"bench_id", "passages": [...]}`; a bare list is accepted too. The `swapped` view
        is the placebo: another cell's passages, by a derangement fixed by the benchmark's seed, so an arm that
        reads text about the wrong ground can be told apart from one that reads the right ground."""
        if view == "swapped":
            other = self.swap_map().get(bench_id)
            return self.passages(other) if other else []
        p = self.dir / "passages" / f"{bench_id}.json"
        if not p.is_file():
            return []
        raw = json.loads(p.read_text())
        return list(raw.get("passages") or []) if isinstance(raw, dict) else list(raw)

    @cached_property
    def _swap(self) -> dict[str, str]:
        from ..bench.celltext import swapped

        have = {p.stem: [1] for p in (self.dir / "passages").glob("*.json")}
        return swapped(have, int(self.manifest.get("seed") or 0))

    def swap_map(self) -> dict[str, str]:
        """Which cell's passages each cell is given in the placebo view (`celltext.swapped`)."""
        return self._swap

    def blind(self, bench_id: str) -> list[Any]:
        p = self.dir / "blind" / f"{bench_id}.json"
        if not p.is_file():
            return []
        raw = json.loads(p.read_text())
        return list(raw.get("files") or []) if isinstance(raw, dict) else list(raw)

    def built_switches(self) -> dict[str, bool | None]:
        """Which parts the benchmark was built with, from the spec in its manifest: the pack's three switches
        and whether the card draws drillholes. None where the manifest does not say."""
        spec = self.manifest.get("spec") if isinstance(self.manifest.get("spec"), dict) else {}
        pack = spec.get("pack") if isinstance(spec.get("pack"), dict) else {}
        card = spec.get("card") if isinstance(spec.get("card"), dict) else {}
        out: dict[str, bool | None] = {k: (bool(pack[k]) if k in pack else None)
                                       for k in ("label_context", "oof_scores", "effort_features")}
        out |= {k: bool(pack.get(k, False)) for k in LATER_SWITCHES}
        out["drillholes"] = bool(card["drillholes"]) if "drillholes" in card else None
        out["drillholes_variant"] = bool(card.get("drillholes_variant", False))
        return out


def compatibility(bench: Bench, switches: Any) -> list[str]:
    """Why an arm cannot honestly run on this benchmark. A pack part the arm switches on must have been built
    into the packs; a part it switches off is removed at run time, except drillholes, which are drawn on the
    card and cannot be taken off an image."""
    built = bench.built_switches()
    problems = []
    for name in ("label_context", "oof_scores", "effort_features"):
        want, have = bool(getattr(switches, name)), built.get(name)
        if want and have is False:
            problems.append(f"the arm asks for {name}, which benchmark {bench.version} was built without")
    # a switch added after the first benchmarks is absent from their manifests, and absent means not built
    for name in LATER_SWITCHES:
        if getattr(switches, name, False) and not built.get(name):
            problems.append(f"the arm asks for {name}, which benchmark {bench.version} was built without")
    want_holes, drawn, variant = bool(switches.drillholes), built.get("drillholes"), built.get("drillholes_variant")
    if want_holes and drawn is False and not variant:
        problems.append(f"the arm asks for drillholes on the card, which benchmark {bench.version} was built without")
    if not want_holes and drawn is True:
        problems.append(f"benchmark {bench.version} draws drillholes on its cards; an arm cannot switch them off")
    return problems


def _heldout_ids(raw: Any) -> set[str]:
    """`heldout.json` as a list of ids, or a dict carrying one under a usual name."""
    if isinstance(raw, list):
        return {str(x) for x in raw}
    if isinstance(raw, dict):
        for k in ("bench_ids", "cells", "heldout", "ids"):
            if isinstance(raw.get(k), list):
                return {str(x) for x in raw[k]}
    return set()


def load_bench(version: str) -> Bench:
    d = bench_dir(version)
    manifest_path = d / "manifest.json"
    if not manifest_path.is_file():
        raise FileNotFoundError(f"no benchmark {version!r}: {manifest_path} is missing; "
                                + hint(f"ue bench build --version {version}"))
    manifest = json.loads(manifest_path.read_text())
    cells = tuple(json.loads(line) for line in (d / "cells.jsonl").read_text().splitlines() if line.strip())
    key_path = d / "key.json"
    key = json.loads(key_path.read_text()) if key_path.is_file() else {}
    heldout = {c["bench_id"] for c in cells if c.get("split") == "heldout"}
    heldout |= {b for b, k in key.items() if k.get("split") == "heldout"}
    heldout_path = d / "heldout.json"
    if heldout_path.is_file():
        heldout |= _heldout_ids(json.loads(heldout_path.read_text()))
    return Bench(
        version=version, dir=d, manifest=manifest,
        manifest_sha256=str(manifest.get("manifest_sha256") or sha256_file(manifest_path)),
        cells=cells, key=key, heldout=frozenset(heldout),
    )


def hashed_files(bench: Bench) -> dict[str, str]:
    """The manifest's file hashes, under `sha256` or `files`; empty when it carries none."""
    hashes = bench.manifest.get("sha256") or bench.manifest.get("files")
    return {str(k): v for k, v in hashes.items() if isinstance(v, str)} if isinstance(hashes, dict) else {}


def verify(bench: Bench) -> list[str]:
    """Files whose hash no longer matches the manifest. Empty means frozen, or nothing to check against:
    the caller asks `hashed_files` to tell those apart."""
    hashes = hashed_files(bench)
    problems = []
    for rel, want in sorted(hashes.items()):
        p = bench.dir / rel
        if not p.is_file():
            problems.append(f"{rel}: missing")
        elif sha256_file(p) != want:
            problems.append(f"{rel}: sha256 differs from the manifest")
    return problems
