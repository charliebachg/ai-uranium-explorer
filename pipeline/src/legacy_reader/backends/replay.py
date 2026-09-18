"""Strict replay: serve recorded envelopes, raise `ReplayMiss` on anything not recorded.

Every test and all UI work runs on this backend, so a missing recording is a loud failure rather than a
silent live call. It reads the same record format `CachedBackend` writes, from any number of roots
(the run cache and the checked-in fixtures under `tests/fixtures/replay/`).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from ..paths import PATHS
from .base import ExtractionRequest, ExtractionResponse, ReplayMiss
from .cache import record_path, response_from_record

FAMILY = "claude_cli"  # replays what the CLI backend recorded, so cache keys match


def fixtures_dir() -> Path:
    return PATHS.pipeline / "tests" / "fixtures" / "replay"


def _index_dir(d: Path) -> dict[str, Path]:
    out: dict[str, Path] = {}
    if not d.is_dir():
        return out
    for p in sorted(d.rglob("*.json")):
        try:
            rec = json.loads(p.read_text())
        except (json.JSONDecodeError, OSError):
            continue
        key = rec.get("cache_key")
        if isinstance(key, str) and isinstance(rec.get("response", {}).get("structured"), dict):
            out.setdefault(key, p)
    return out


class ReplayBackend:
    """Read-only. Never spawns a process, never writes."""

    family = FAMILY

    def __init__(self, roots: list[Path] | None = None, family: str = FAMILY):
        self.family = family
        self.roots = roots if roots is not None else [PATHS.cache, fixtures_dir()]
        self._extra: dict[str, Path] = {}
        for root in self.roots:
            # a cache root holds calls/<k[:2]>/<k>.json; a fixture dir holds loose records
            self._extra.update(_index_dir(root))

    def find(self, key: str) -> Path | None:
        for root in self.roots:
            p = record_path(key, root)
            if p.is_file():
                return p
        return self._extra.get(key)

    def record(self, key: str) -> dict[str, Any]:
        path = self.find(key)
        if path is None:
            raise ReplayMiss(f"no recorded call for cache key {key} in {[str(r) for r in self.roots]}")
        return json.loads(path.read_text())

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        key = req.cache_key(self.family)
        resp = response_from_record(self.record(key), key)
        resp.from_cache = True
        return resp


def pack_run(run_id: str, dest: Path | None = None, cache_root: Path | None = None) -> list[Path]:
    """`lr replay pack --run <id>`: copy a run's call records into tests/fixtures/replay/."""
    from ..extract import run_dir  # local import: extract imports this module

    dest = dest or fixtures_dir()
    cache_root = cache_root or PATHS.cache
    calls_file = run_dir(run_id) / "calls.jsonl"
    if not calls_file.is_file():
        raise FileNotFoundError(f"no run log at {calls_file}")
    dest.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    for line in calls_file.read_text().splitlines():
        if not line.strip():
            continue
        row = json.loads(line)
        key = row.get("cache_key")
        if not key or row.get("status") != "ok":
            continue
        src = record_path(key, cache_root)
        if not src.is_file():
            continue
        name = f"{run_id}_{row.get('file_num', 'x')}_p{int(row.get('page_no') or 0):04d}_{key[:8]}.json"
        out = dest / name
        out.write_text(src.read_text())
        written.append(out)
    return written
