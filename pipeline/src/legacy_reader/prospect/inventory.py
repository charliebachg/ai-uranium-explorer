"""The data inventory: what we may use, what it bears on, and what is missing.

`knowledge/data_inventory.toml` is the register the rest of the prospect work reads. It is validated on load,
because the failure it guards against is silent: a layer used without a licence, a label used as a feature, or
a number relied on that nobody ever verified.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..paths import PATHS

INVENTORY_PATH = PATHS.pipeline / "knowledge" / "data_inventory.toml"

#: mineral-systems vocabulary, restricted to what research reports 01 and 02 support for the Athabasca.
#: `source` and `preservation` are deliberately missing: no public proxy for either was found.
BEARS_ON = frozenset({"pathway", "trap", "detection", "dispersal", "cover", "effort", "label", "study_area"})
TIERS = frozenset({"native", "read", "derived"})
ROLES = frozenset({"feature", "label", "context"})
ACCESS = frozenset({"arcgis_rest", "stac", "file", "internal"})
GAP_STATUS = frozenset({"not_addressable", "not_published", "not_public", "unverified", "published_not_pulled"})

#: the mineral dispositions service is never loaded by this project (an editorial rule, tested for)
BANNED_URL = "Mining/MapServer"


class InventoryError(Exception):
    """The inventory does not describe a usable set of sources."""


@dataclass(frozen=True)
class Licence:
    key: str
    name: str
    url: str
    terms: str
    redistributable: bool


@dataclass(frozen=True)
class Source:
    key: str
    title: str
    tier: str
    role: str
    bears_on: str
    access: str
    url: str
    licence: Licence
    verified: bool
    verified_at: str
    fields_verified: bool
    where: str | None = None
    geometry: str | None = None
    collection: str | None = None
    resolution_m: float | None = None
    record_count: int | None = None
    fields: tuple[str, ...] = ()
    notes: str = ""
    caveats: tuple[str, ...] = ()

    @property
    def is_label(self) -> bool:
        return self.role == "label"

    @property
    def redistributable(self) -> bool:
        return self.licence.redistributable


@dataclass(frozen=True)
class Gap:
    key: str
    title: str
    why_it_matters: str
    status: str
    evidence: str
    workaround: str = ""


@dataclass(frozen=True)
class Inventory:
    schema_version: str
    region_bbox: tuple[float, float, float, float]
    region_note: str
    licences: dict[str, Licence]
    sources: tuple[Source, ...]
    gaps: tuple[Gap, ...] = field(default=())

    def by_key(self, key: str) -> Source:
        for s in self.sources:
            if s.key == key:
                return s
        raise KeyError(key)

    def with_role(self, role: str) -> tuple[Source, ...]:
        return tuple(s for s in self.sources if s.role == role)

    def usable_features(self) -> tuple[Source, ...]:
        """Feature sources confirmed by a live call. An unverified source is not a feature yet."""
        return tuple(s for s in self.sources if s.role == "feature" and s.verified)


def _require(d: dict[str, Any], keys: tuple[str, ...], where: str) -> None:
    missing = [k for k in keys if d.get(k) in (None, "")]
    if missing:
        raise InventoryError(f"{where}: missing {', '.join(missing)}")


def load(path: Path | None = None) -> Inventory:
    raw = tomllib.loads((path or INVENTORY_PATH).read_text())
    licences = {
        key: Licence(
            key=key,
            name=str(v.get("name") or ""),
            url=str(v.get("url") or ""),
            terms=str(v.get("terms") or ""),
            redistributable=bool(v.get("redistributable")),
        )
        for key, v in (raw.get("licence") or {}).items()
    }
    if not licences:
        raise InventoryError("no [licence.*] blocks: every source needs a licence to point at")
    for lic in licences.values():
        _require({"name": lic.name, "terms": lic.terms}, ("name", "terms"), f"licence.{lic.key}")

    sources: list[Source] = []
    seen: set[str] = set()
    for entry in raw.get("source") or []:
        key = str(entry.get("key") or "")
        where = f"source {key or '<no key>'}"
        _require(entry, ("key", "title", "tier", "role", "bears_on", "access", "url", "licence",
                         "verified_at"), where)
        if key in seen:
            raise InventoryError(f"{where}: duplicate key")
        seen.add(key)
        if entry["tier"] not in TIERS:
            raise InventoryError(f"{where}: tier {entry['tier']!r} not one of {sorted(TIERS)}")
        if entry["role"] not in ROLES:
            raise InventoryError(f"{where}: role {entry['role']!r} not one of {sorted(ROLES)}")
        if entry["bears_on"] not in BEARS_ON:
            raise InventoryError(f"{where}: bears_on {entry['bears_on']!r} not one of {sorted(BEARS_ON)}")
        if entry["access"] not in ACCESS:
            raise InventoryError(f"{where}: access {entry['access']!r} not one of {sorted(ACCESS)}")
        if entry["licence"] not in licences:
            raise InventoryError(f"{where}: licence {entry['licence']!r} has no [licence.*] block")
        if BANNED_URL in str(entry["url"]):
            raise InventoryError(f"{where}: {BANNED_URL} is never loaded by this project")
        sources.append(Source(
            key=key, title=str(entry["title"]), tier=str(entry["tier"]), role=str(entry["role"]),
            bears_on=str(entry["bears_on"]), access=str(entry["access"]), url=str(entry["url"]),
            licence=licences[str(entry["licence"])], verified=bool(entry.get("verified")),
            verified_at=str(entry["verified_at"]), fields_verified=bool(entry.get("fields_verified")),
            where=entry.get("where"), geometry=entry.get("geometry"), collection=entry.get("collection"),
            resolution_m=entry.get("resolution_m"), record_count=entry.get("record_count"),
            fields=tuple(entry.get("fields") or ()), notes=str(entry.get("notes") or ""),
            caveats=tuple(entry.get("caveats") or ()),
        ))
    if not sources:
        raise InventoryError("no [[source]] blocks")

    gaps: list[Gap] = []
    for entry in raw.get("gap") or []:
        where = f"gap {entry.get('key') or '<no key>'}"
        _require(entry, ("key", "title", "why_it_matters", "status", "evidence"), where)
        if entry["status"] not in GAP_STATUS:
            raise InventoryError(f"{where}: status {entry['status']!r} not one of {sorted(GAP_STATUS)}")
        gaps.append(Gap(
            key=str(entry["key"]), title=str(entry["title"]),
            why_it_matters=str(entry["why_it_matters"]), status=str(entry["status"]),
            evidence=str(entry["evidence"]), workaround=str(entry.get("workaround") or ""),
        ))

    bbox = tuple(raw.get("region_bbox") or ())
    if len(bbox) != 4:
        raise InventoryError("region_bbox must be [west, south, east, north]")
    return Inventory(
        schema_version=str(raw.get("schema_version") or "0"),
        region_bbox=(float(bbox[0]), float(bbox[1]), float(bbox[2]), float(bbox[3])),
        region_note=str(raw.get("region_note") or ""),
        licences=licences, sources=tuple(sources), gaps=tuple(gaps),
    )


def summary(inv: Inventory | None = None) -> dict[str, Any]:
    """Counts for `lr prospect inventory` and, later, the readiness page."""
    inv = inv or load()
    return {
        "sources": len(inv.sources),
        "features": len(inv.with_role("feature")),
        "features_verified": len(inv.usable_features()),
        "labels": len(inv.with_role("label")),
        "context": len(inv.with_role("context")),
        "redistributable": sum(1 for s in inv.sources if s.redistributable),
        "tier_read": sum(1 for s in inv.sources if s.tier == "read"),
        "gaps": len(inv.gaps),
        "bears_on": sorted({s.bears_on for s in inv.sources}),
    }
