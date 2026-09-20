"""The interface benchmark's spec: targets per tier and kind, the seed, and the analyst benchmark it draws
cells from. One TOML per version under `configs/bench/interface-<version>.toml`, loaded strictly: a key the
spec does not know is an error, and a kind or a source the generators do not implement is an error too,
because a misspelt target that quietly fell back to zero would be a benchmark nobody meant to build.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..spec import CONFIG_DIR, SpecError

SPLITS = ("open", "all")


@dataclass(frozen=True)
class Tier1Spec:
    kinds: dict[str, int] = field(default_factory=dict)
    nearby_radius_m: float = 5000.0
    label_radius_km: float = 25.0
    nearby_layers: tuple[str, ...] = ("em_conductors", "faults_250k", "radioactive_boulders",
                                      "lake_sediment_gsc", "lake_sediment_sgs")


@dataclass(frozen=True)
class Tier3Spec:
    sources: dict[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class InterfaceSpec:
    version: str
    seed: int
    analyst_version: str
    split: str
    tier1: Tier1Spec
    tier3: Tier3Spec

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


_TOP = {"version", "seed", "analyst_version", "split", "tier1", "tier3"}
_TIER1 = {"kinds", "nearby_radius_m", "label_radius_km", "nearby_layers"}
_TIER3 = {"sources"}


def spec_path(version: str) -> Path:
    return CONFIG_DIR / f"interface-{version}.toml"


def load_spec(version: str, path: Path | None = None) -> InterfaceSpec:
    p = path or spec_path(version)
    if not p.is_file():
        raise SpecError(f"no spec for interface benchmark {version!r} at {p}")
    return parse_spec(tomllib.loads(p.read_text()), version)


def parse_spec(raw: dict[str, Any], version: str | None = None) -> InterfaceSpec:
    """Pure: a parsed TOML mapping into a validated spec. The kind and source names are checked against the
    generators here so the spec cannot ask for something nothing can make."""
    from .tier1 import KINDS
    from .tier3 import SOURCES

    unknown = set(raw) - _TOP
    if unknown:
        raise SpecError(f"unknown top-level key(s): {sorted(unknown)}")
    missing = {"version", "seed", "analyst_version", "tier1", "tier3"} - set(raw)
    if missing:
        raise SpecError(f"missing key(s): {sorted(missing)}")
    t1, t3 = raw["tier1"], raw["tier3"]
    if not isinstance(t1, dict) or not isinstance(t3, dict):
        raise SpecError("[tier1] and [tier3] must be tables")
    bad = set(t1) - _TIER1
    if bad:
        raise SpecError(f"[tier1]: unknown key(s) {sorted(bad)}")
    bad = set(t3) - _TIER3
    if bad:
        raise SpecError(f"[tier3]: unknown key(s) {sorted(bad)}")
    kinds = _targets(t1.get("kinds", {}), "tier1.kinds", set(KINDS))
    sources = _targets(t3.get("sources", {}), "tier3.sources", set(SOURCES))
    layers = tuple(str(k) for k in t1.get("nearby_layers", Tier1Spec.nearby_layers))
    spec = InterfaceSpec(
        version=str(raw["version"]), seed=int(raw["seed"]), analyst_version=str(raw["analyst_version"]),
        split=str(raw.get("split", "open")),
        tier1=Tier1Spec(kinds=kinds, nearby_radius_m=float(t1.get("nearby_radius_m", 5000.0)),
                        label_radius_km=float(t1.get("label_radius_km", 25.0)), nearby_layers=layers),
        tier3=Tier3Spec(sources=sources),
    )
    _validate(spec, version)
    return spec


def _targets(body: Any, where: str, known: set[str]) -> dict[str, int]:
    if not isinstance(body, dict):
        raise SpecError(f"[{where}] must be a table of name = count")
    bad = set(body) - known
    if bad:
        raise SpecError(f"[{where}]: unknown name(s) {sorted(bad)}; known: {sorted(known)}")
    out: dict[str, int] = {}
    for k, v in body.items():
        if isinstance(v, bool) or not isinstance(v, int) or v < 0:
            raise SpecError(f"[{where}] {k} must be a non-negative integer")
        out[str(k)] = int(v)
    return out


def _validate(spec: InterfaceSpec, version: str | None) -> None:
    from ...prospect.tools import NEARBY_LAYERS, NEARBY_RADIUS_M

    if version is not None and spec.version != version:
        raise SpecError(f"spec says version {spec.version!r} but the file is {version!r}")
    if spec.split not in SPLITS:
        raise SpecError(f"split must be one of {SPLITS}, not {spec.split!r}")
    lo, hi = NEARBY_RADIUS_M
    if not lo <= spec.tier1.nearby_radius_m <= hi:
        raise SpecError(f"[tier1] nearby_radius_m must lie in [{lo:g}, {hi:g}]")
    if spec.tier1.label_radius_km <= 0:
        raise SpecError("[tier1] label_radius_km must be positive")
    for layer in spec.tier1.nearby_layers:
        if layer not in NEARBY_LAYERS:
            raise SpecError(f"[tier1] {layer!r} is not an evidence layer `nearby` opens")
    if not spec.tier1.kinds and not spec.tier3.sources:
        raise SpecError("the spec asks for nothing")
