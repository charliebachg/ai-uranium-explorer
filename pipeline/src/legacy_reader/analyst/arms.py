"""Arm configurations: one TOML per question the benchmark is asked.

An arm is everything that may differ between two runs of the analyst over the same frozen benchmark: the
model, its effort, which inputs it is shown and which parts of the evidence pack are switched on. The headline
arm (`v0`) shows the map card and the pack with every switch off; every other arm changes exactly one thing
and says in its `notes` line which question that answers. Keeping the whole configuration in a file, with
every switch stated rather than defaulted, is what lets the table say "this row differs from that one in
exactly this" without anyone reading code.
"""

from __future__ import annotations

import tomllib
from dataclasses import asdict, dataclass, fields
from pathlib import Path
from typing import Any

from ..paths import PATHS

AGENTS = ("v0",)
EFFORTS = ("low", "medium", "high", "xhigh", "max")


@dataclass(frozen=True)
class Inputs:
    """What the model is handed."""

    card: bool        # the map card, as an image
    pack: bool        # the evidence pack, as text
    passages: bool    # retrieved passages from the assessment record


@dataclass(frozen=True)
class Switches:
    """Which parts of the evidence pack are shown. Off means removed before rendering, not greyed out."""

    drillholes: bool        # the drillhole rows read from the assessment reports
    label_context: bool     # distance to the nearest known deposit or occurrence
    oof_scores: bool        # the out-of-fold fitted scores (learned, effort, criteria)
    effort_features: bool   # the exploration-effort features (hole and sample counts)

    def on(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self) if getattr(self, f.name))


@dataclass(frozen=True)
class ArmConfig:
    name: str
    agent: str
    model: str
    effort: str
    inputs: Inputs
    switches: Switches
    prompt_version: str
    max_budget_usd_per_call: float
    timeout_s: int
    workers: int
    notes: str

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def arms_dir() -> Path:
    return PATHS.pipeline / "configs" / "arms"


def _table(raw: dict[str, Any], key: str, cls: type) -> Any:
    """A sub-table with exactly the dataclass's keys: an arm that forgets a switch is not an arm."""
    sub = raw.get(key)
    if not isinstance(sub, dict):
        raise ValueError(f"[arm.{key}] table is missing")
    want = {f.name for f in fields(cls)}
    missing, extra = want - set(sub), set(sub) - want
    if missing or extra:
        raise ValueError(f"[arm.{key}]: missing {sorted(missing)}, unknown {sorted(extra)}")
    for k, v in sub.items():
        if not isinstance(v, bool):
            raise ValueError(f"[arm.{key}].{k} must be true or false, not {v!r}")
    return cls(**sub)


def parse_arm(raw: dict[str, Any]) -> ArmConfig:
    """An ArmConfig from a parsed TOML document, refusing unknown or missing keys."""
    arm = raw.get("arm")
    if not isinstance(arm, dict):
        raise ValueError("an arm file has one [arm] table")
    scalar = {f.name for f in fields(ArmConfig)} - {"inputs", "switches"}
    missing = scalar - set(arm)
    extra = set(arm) - scalar - {"inputs", "switches"}
    if missing or extra:
        raise ValueError(f"[arm]: missing {sorted(missing)}, unknown {sorted(extra)}")
    if arm["agent"] not in AGENTS:
        raise ValueError(f"agent {arm['agent']!r} is not one of {AGENTS}")
    if arm["effort"] not in EFFORTS:
        raise ValueError(f"effort {arm['effort']!r} is not one of {EFFORTS}")
    if not str(arm["notes"]).strip():
        raise ValueError("an arm must say in `notes` which question it answers")
    return ArmConfig(
        name=str(arm["name"]), agent=str(arm["agent"]), model=str(arm["model"]), effort=str(arm["effort"]),
        inputs=_table(arm, "inputs", Inputs), switches=_table(arm, "switches", Switches),
        prompt_version=str(arm["prompt_version"]), max_budget_usd_per_call=float(arm["max_budget_usd_per_call"]),
        timeout_s=int(arm["timeout_s"]), workers=int(arm["workers"]), notes=str(arm["notes"]),
    )


def load_arm(name: str, path: Path | None = None) -> ArmConfig:
    """`configs/arms/<name>.toml`. The file's `name` must match its stem, so a copied file cannot masquerade."""
    path = path or arms_dir() / f"{name}.toml"
    if not path.is_file():
        raise FileNotFoundError(f"no arm {name!r} in {path.parent} (have: {list_arms(path.parent)})")
    arm = parse_arm(tomllib.loads(path.read_text()))
    if arm.name != path.stem:
        raise ValueError(f"{path.name} says its name is {arm.name!r}")
    return arm


def list_arms(directory: Path | None = None) -> list[str]:
    d = directory or arms_dir()
    return sorted(p.stem for p in d.glob("*.toml")) if d.is_dir() else []
