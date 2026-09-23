"""The pilot's cells, drawn by the rule benchmark v3's pre-registration fixes: open labelled cells that have
passages, a fixed number of positives and of negatives, each drawn with numpy's `default_rng(seed)` from its
class in bench-id order. Written beside the benchmark's results, so the main run can leave them out."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import numpy as np

from ..analyst import frozen as F
from ..analyst.score import POSITIVE_LABELS, out_dir

PILOT_SEED = 20260924


def draw(version: str, n_pos: int = 15, n_neg: int = 15, seed: int = PILOT_SEED) -> dict[str, Any]:
    bench = F.load_bench(version)
    have = [c["bench_id"] for c in sorted(bench.open_cells(), key=lambda c: c["bench_id"])
            if bench.passages(c["bench_id"]) and bench.key[c["bench_id"]].get("stratum") != "probe"]
    pos = [b for b in have if bench.key[b].get("label") in POSITIVE_LABELS]
    neg = [b for b in have if bench.key[b].get("label") not in POSITIVE_LABELS]
    rng = np.random.default_rng(seed)
    picked = sorted([*rng.choice(pos, size=min(n_pos, len(pos)), replace=False).tolist(),
                     *rng.choice(neg, size=min(n_neg, len(neg)), replace=False).tolist()])
    return {"version": version, "seed": seed, "rule": f"{n_pos} positives and {n_neg} negatives among the "
            "open labelled cells with passages, drawn per class in bench-id order", "cells": picked,
            "eligible": {"positives": len(pos), "negatives": len(neg)}}


def write(version: str, pilot: dict[str, Any]) -> Path:
    path = out_dir(version) / "pilot.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(pilot, indent=1) + "\n")
    return path


__all__ = ["PILOT_SEED", "draw", "write"]
