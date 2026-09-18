"""Filesystem layout. Every path the pipeline writes is derived here."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path


def _project_root() -> Path:
    env = os.environ.get("LR_ROOT")
    if env:
        return Path(env).resolve()
    # src/legacy_reader/paths.py -> pipeline/ -> legacy-reader/
    return Path(__file__).resolve().parents[3]


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def pipeline(self) -> Path:
        return self.root / "pipeline"

    @property
    def data(self) -> Path:
        return self.pipeline / "data"

    @property
    def grids(self) -> Path:
        return self.data / "grids"

    @property
    def index(self) -> Path:
        return self.data / "index"

    @property
    def raw(self) -> Path:
        return self.data / "raw"

    @property
    def pages(self) -> Path:
        return self.data / "pages"

    @property
    def ocr(self) -> Path:
        return self.data / "ocr"

    @property
    def ref(self) -> Path:
        return self.data / "ref"

    @property
    def cache(self) -> Path:
        return self.data / "cache"

    @property
    def runs(self) -> Path:
        return self.data / "runs"

    @property
    def out(self) -> Path:
        return self.data / "out"

    @property
    def gold(self) -> Path:
        return self.root / "gold"

    @property
    def web_data(self) -> Path:
        return self.root / "web" / "public" / "data"

    @property
    def grids_lock(self) -> Path:
        return self.pipeline / "grids.lock"


PATHS = Paths(_project_root())
