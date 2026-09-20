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

AGENTS = ("v0", "v1")
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
    criteria: bool          # the criteria table with its fuzzy memberships; off, the model reasons from features

    def on(self) -> tuple[str, ...]:
        return tuple(f.name for f in fields(self) if getattr(self, f.name))


@dataclass(frozen=True)
class LoopSpec:
    """The staged loop's switches (PRD §8.4 and §8.5), one table per v1 arm. Every field is stated in the file,
    as the pack switches are, so the arms table can say in what exactly two v1 rows differ. The enums are
    checked where they are used, by `analyst.loop.LoopConfig`, so this module stays free of the loop."""

    executor_model: str
    verifier_model: str
    adjudicator_model: str
    planner_model: str
    planner: str            # template | model
    verifier: str           # none | skeptic
    rounds: int             # K, the refinement rounds
    triage: bool
    executor_context: str   # independent | cumulative
    decider: str            # both | weighted | adjudicator
    segment_workers: int
    retrieval: bool
    skip_unmeasured: bool   # a criterion with no value here is settled unknown by the harness, no executor call
    executor_batch: bool    # one executor call over every criterion instead of one per segment

    def config(self, effort: str, prompt_version: str) -> Any:
        """The loop's own configuration object; imported here so an arm file can be parsed without the loop."""
        from .loop import LoopConfig

        return LoopConfig(executor_model=self.executor_model, verifier_model=self.verifier_model,
                          adjudicator_model=self.adjudicator_model, planner_model=self.planner_model,
                          effort=effort, planner=self.planner, verifier=self.verifier, rounds=self.rounds,
                          triage=self.triage, executor_context=self.executor_context, decider=self.decider,
                          segment_workers=self.segment_workers, retrieval=self.retrieval,
                          skip_unmeasured=self.skip_unmeasured, executor_batch=self.executor_batch,
                          prompt_version=prompt_version)


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
    loop: LoopSpec | None = None   # required for agent v1, refused for v0

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)


def arms_dir() -> Path:
    return PATHS.pipeline / "configs" / "arms"


def _table(raw: dict[str, Any], key: str, cls: type, booleans: bool = True) -> Any:
    """A sub-table with exactly the dataclass's keys: an arm that forgets a switch is not an arm. The two
    switch tables hold booleans only; the loop table is typed by its dataclass."""
    sub = raw.get(key)
    if not isinstance(sub, dict):
        raise ValueError(f"[arm.{key}] table is missing")
    want = {f.name for f in fields(cls)}
    missing, extra = want - set(sub), set(sub) - want
    if missing or extra:
        raise ValueError(f"[arm.{key}]: missing {sorted(missing)}, unknown {sorted(extra)}")
    if booleans:
        for k, v in sub.items():
            if not isinstance(v, bool):
                raise ValueError(f"[arm.{key}].{k} must be true or false, not {v!r}")
        return cls(**sub)
    types = {f.name: f.type for f in fields(cls)}
    for k, v in sub.items():
        want_t = {"str": str, "int": int, "bool": bool}[str(types[k])]
        if not isinstance(v, want_t) or (want_t is int and isinstance(v, bool)):
            raise ValueError(f"[arm.{key}].{k} must be {want_t.__name__}, not {v!r}")
    return cls(**sub)


def parse_arm(raw: dict[str, Any]) -> ArmConfig:
    """An ArmConfig from a parsed TOML document, refusing unknown or missing keys."""
    arm = raw.get("arm")
    if not isinstance(arm, dict):
        raise ValueError("an arm file has one [arm] table")
    scalar = {f.name for f in fields(ArmConfig)} - {"inputs", "switches", "loop"}
    missing = scalar - set(arm)
    extra = set(arm) - scalar - {"inputs", "switches", "loop"}
    if missing or extra:
        raise ValueError(f"[arm]: missing {sorted(missing)}, unknown {sorted(extra)}")
    if arm["agent"] not in AGENTS:
        raise ValueError(f"agent {arm['agent']!r} is not one of {AGENTS}")
    loop = None
    if arm["agent"] == "v1":
        loop = _table(arm, "loop", LoopSpec, booleans=False)
    elif "loop" in arm:
        raise ValueError(f"agent {arm['agent']!r} has no loop; the [arm.loop] table belongs to v1 arms")
    if arm["effort"] not in EFFORTS:
        raise ValueError(f"effort {arm['effort']!r} is not one of {EFFORTS}")
    if not str(arm["notes"]).strip():
        raise ValueError("an arm must say in `notes` which question it answers")
    return ArmConfig(
        name=str(arm["name"]), agent=str(arm["agent"]), model=str(arm["model"]), effort=str(arm["effort"]),
        inputs=_table(arm, "inputs", Inputs), switches=_table(arm, "switches", Switches),
        prompt_version=str(arm["prompt_version"]), max_budget_usd_per_call=float(arm["max_budget_usd_per_call"]),
        timeout_s=int(arm["timeout_s"]), workers=int(arm["workers"]), notes=str(arm["notes"]), loop=loop,
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
