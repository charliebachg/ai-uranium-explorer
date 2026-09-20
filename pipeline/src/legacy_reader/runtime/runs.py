"""Run ids and run directories, shared by every kind of run.

A run id is a UTC timestamp followed by what the run is (`20260920T120000Z-memo`), so a directory listing
sorts into a timeline and reads as one. Every run's files — manifest, call log, spans — live in one directory
under `data/runs/`, which is what `lr usage` and the replay packer already expect of an extract run.
"""

from __future__ import annotations

import datetime as dt
from pathlib import Path

from ..paths import PATHS


def new_run_id(kind: str) -> str:
    """A fresh run id: the UTC second plus the run's kind (or, for an extract, its config id)."""
    return dt.datetime.now(dt.UTC).strftime("%Y%m%dT%H%M%SZ") + f"-{kind}"


def run_dir(run_id: str) -> Path:
    """Where a run's files go. Not created here: the run creates it when it first writes."""
    return PATHS.runs / run_id
