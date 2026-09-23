"""The benchmark spec: one TOML file per version, and the only place a number about the benchmark lives.

A benchmark version is its spec. The sampler, the cards, the packs, the blind-lists and the manifest all read
from this dataclass and nothing else, so the next version is a new file under `configs/bench/` and the same
commands. Loading is strict: a key the spec does not know is an error rather than a silent no-op, because a
misspelt `held_out_share` that quietly fell back to a default would be a benchmark nobody meant to build.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from ..paths import PATHS

CONFIG_DIR = PATHS.pipeline / "configs" / "bench"

STRATA = ("deposit", "occurrence", "negative", "probe")

#: layers a card may draw, and the pulled layer each one is read from. Deposit and occurrence layers are not
#: here on purpose: a card that drew the labels would be the answer key with a border round it.
CARD_LAYERS: dict[str, str] = {
    "em_conductors": "em_conductors",
    "faults_250k": "faults_250k",
    "graphitic_host": "bedrock_250k",
    "lake_sediment_gsc": "lake_sediment_gsc",
    "lake_sediment_sgs": "lake_sediment_sgs",
    "radioactive_boulders": "radioactive_boulders",
    "survey_footprints_ground": "survey_footprints_ground",
    "survey_footprints_airborne": "survey_footprints_airborne",
}
LABEL_LAYERS = ("uranium_deposit_footprints", "mineral_deposits_uranium")


class SpecError(Exception):
    """The spec file does not describe a benchmark this builder can make."""


@dataclass(frozen=True)
class Strata:
    deposit: int
    occurrence: int
    negative: int
    probe: int
    thin_block_m: float = 10_000.0     # deposits: one per block of this size
    min_holes: int = 5                 # negatives: at least this many holes, so "nobody looked" is excluded

    def asked(self, stratum: str) -> int:
        return int(getattr(self, stratum))


@dataclass(frozen=True)
class CardSpec:
    window_km: float
    size_px: int
    layers: tuple[str, ...]
    drillholes: bool = False
    #: also render every card a second time with drillholes drawn, under cards_drillholes/, so an arm can
    #: switch effort on without a second benchmark; an image cannot be un-drawn, so both are built
    drillholes_variant: bool = False


#: pack switches added after v1. Off, they are left out of the switches and the manifest, so a spec written
#: before them builds and hashes exactly as it did.
LATER_PACK_SWITCHES = ("evidence", "region", "extended_features")


@dataclass(frozen=True)
class PackSpec:
    label_context: bool = False
    oof_scores: bool = False
    effort_features: bool = False
    #: the raw evidence around the cell, one tool per family (`prospect.evidence`)
    evidence: bool = False
    #: the regional setting: the sandstone cover, basement domains by letter, the nearest domain boundary
    region: bool = False
    #: the extended features in `cell_features` and `coverage`, and the extended model's out-of-fold score
    extended_features: bool = False

    def switches(self) -> dict[str, bool]:
        return {k: v for k, v in asdict(self).items() if v or k not in LATER_PACK_SWITCHES}

    def later(self) -> bool:
        """Whether any switch added after v1 is on: such a pack is also scrubbed of place names."""
        return any(getattr(self, k) for k in LATER_PACK_SWITCHES)


@dataclass(frozen=True)
class BlindSpec:
    radius_km: float


@dataclass(frozen=True)
class RetrievalSpec:
    k: int
    radius_km: float
    query: str = "uranium mineralization drilling graphitic conductor unconformity alteration"


@dataclass(frozen=True)
class BenchSpec:
    version: str
    seed: int
    strata: Strata
    fold_km: float
    n_folds: int
    held_out_share: float
    card: CardSpec
    pack: PackSpec = field(default_factory=PackSpec)
    blind: BlindSpec = field(default_factory=lambda: BlindSpec(10.0))
    retrieval: RetrievalSpec = field(default_factory=lambda: RetrievalSpec(6, 40.0))

    def as_dict(self) -> dict[str, Any]:
        """The spec as plain data, for the manifest; pack switches added later appear only when on."""
        return {**asdict(self), "pack": self.pack.switches()}


# ---------------------------------------------------------------- loading


_TOP = {"version", "seed", "strata", "fold_km", "n_folds", "held_out_share", "card", "pack", "blind", "retrieval"}
_SECTIONS: dict[str, type] = {"strata": Strata, "card": CardSpec, "pack": PackSpec, "blind": BlindSpec,
                              "retrieval": RetrievalSpec}


def spec_path(version: str) -> Path:
    return CONFIG_DIR / f"{version}.toml"


def load_spec(version: str, path: Path | None = None) -> BenchSpec:
    """Read and validate `configs/bench/<version>.toml`. Unknown keys anywhere are errors."""
    p = path or spec_path(version)
    if not p.is_file():
        raise SpecError(f"no spec for benchmark {version!r} at {p}")
    return parse_spec(tomllib.loads(p.read_text()), version)


def parse_spec(raw: dict[str, Any], version: str | None = None) -> BenchSpec:
    """Pure: a parsed TOML mapping into a validated spec."""
    unknown = set(raw) - _TOP
    if unknown:
        raise SpecError(f"unknown top-level key(s): {sorted(unknown)}")
    missing = {"version", "seed", "strata", "fold_km", "n_folds", "held_out_share", "card"} - set(raw)
    if missing:
        raise SpecError(f"missing key(s): {sorted(missing)}")
    sections: dict[str, Any] = {}
    for name, cls in _SECTIONS.items():
        if name not in raw:
            continue
        body = raw[name]
        if not isinstance(body, dict):
            raise SpecError(f"[{name}] must be a table")
        allowed = set(cls.__dataclass_fields__)
        bad = set(body) - allowed
        if bad:
            raise SpecError(f"[{name}]: unknown key(s) {sorted(bad)}")
        try:
            sections[name] = cls(**body)
        except TypeError as err:
            raise SpecError(f"[{name}]: {err}") from err
    spec = BenchSpec(
        version=str(raw["version"]), seed=int(raw["seed"]), strata=sections["strata"],
        fold_km=float(raw["fold_km"]), n_folds=int(raw["n_folds"]), held_out_share=float(raw["held_out_share"]),
        card=_card(sections["card"]), **{k: v for k, v in sections.items() if k in ("pack", "blind", "retrieval")},
    )
    _validate(spec, version)
    return spec


def _card(card: CardSpec) -> CardSpec:
    layers = tuple(str(k) for k in card.layers)
    return CardSpec(window_km=float(card.window_km), size_px=int(card.size_px), layers=layers,
                    drillholes=bool(card.drillholes), drillholes_variant=bool(getattr(card, "drillholes_variant", False)))


def _validate(spec: BenchSpec, version: str | None) -> None:
    if version is not None and spec.version != version:
        raise SpecError(f"spec says version {spec.version!r} but the file is {version!r}")
    for s in STRATA:
        if spec.strata.asked(s) < 0:
            raise SpecError(f"[strata] {s} must be non-negative")
    if spec.strata.thin_block_m <= 0 or spec.strata.min_holes < 0:
        raise SpecError("[strata] thin_block_m must be positive and min_holes non-negative")
    if spec.fold_km <= 0 or spec.n_folds < 2:
        raise SpecError("fold_km must be positive and n_folds at least 2")
    if not 0.0 <= spec.held_out_share <= 1.0:
        raise SpecError("held_out_share must lie in [0, 1]")
    if spec.card.window_km <= 0 or spec.card.size_px < 100:
        raise SpecError("[card] window_km must be positive and size_px at least 100")
    for key in spec.card.layers:
        if key in LABEL_LAYERS:
            raise SpecError(f"[card] {key} is a label layer and is never drawn")
        if key not in CARD_LAYERS:
            raise SpecError(f"[card] unknown layer {key!r}; known: {sorted(CARD_LAYERS)}")
    if spec.blind.radius_km <= 0 or spec.retrieval.radius_km <= 0 or spec.retrieval.k < 1:
        raise SpecError("[blind] and [retrieval] radii must be positive and k at least 1")
    if not spec.retrieval.query.strip():
        raise SpecError("[retrieval] query must not be empty")
