"""The run manifest: what a run was asked, of which models, against which store, for how much.

The rule is that a run is replayable from its manifest and the call cache. That needs the manifest to name
everything the answer depended on — the prompt and schema hashes (the cache key covers the same texts), the
model behind each role, the store hash and the snapshot it is, the git commit, the benchmark manifest and the
score versions the agent saw, the retrieval blind-list, the seed — and everything the bill depended on: the
budget the run was given and what it spent. A field the run does not use stays None rather than being left
out, so every manifest has the same shape and a reader never has to know which kind wrote it.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field, fields
from pathlib import Path
from typing import Any

from .runs import new_run_id
from .spend import RunBudget

KINDS = ("extract", "memo", "chat", "bench", "other")


def _write_atomic(path: Path, text: str) -> None:
    from ..backends.cache import write_atomic  # local: backends.cache imports this package

    write_atomic(path, text)


@dataclass
class Manifest:
    run_id: str
    kind: str
    started_at: str
    finished_at: str | None = None
    config: dict[str, Any] = field(default_factory=dict)
    models: dict[str, str] = field(default_factory=dict)          # role -> full model id
    prompt_hashes: dict[str, str] = field(default_factory=dict)   # prompt name -> sha256 of its text
    schema_hashes: dict[str, str] = field(default_factory=dict)   # schema name -> sha256 of its canonical JSON
    store_sha256: str | None = None
    snapshot: str | None = None
    git_commit: str | None = None
    bench: dict[str, Any] | None = None                          # bench_id, manifest_sha256
    scores_seen: list[dict[str, Any]] = field(default_factory=list)   # model_version, fold
    blind_list_sha256: str | None = None
    seed: int | None = None
    budget_usd: float | None = None
    spent_usd: float = 0.0
    notes: str = ""

    # ---- lifecycle

    @classmethod
    def start(cls, kind: str, config: dict[str, Any], models: dict[str, str], seed: int | None = None,
              budget_usd: float | None = None, bench: dict[str, Any] | None = None,
              run_id: str | None = None) -> "Manifest":
        """A manifest for a run starting now, naming the store as it is on disk and the commit it runs from.

        A machine with no store file (a fresh checkout, a test on an injected frame) records None for both the
        hash and the snapshot rather than failing: the run still happened, it just names no data."""
        from ..store import snapshot as SN

        try:
            pinned = SN.pin(None, log=lambda *a: None)
        except (OSError, RuntimeError):
            pinned = {"store_sha256": None, "snapshot": None}
        return cls(
            run_id=run_id or new_run_id(kind), kind=kind,
            started_at=dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            config=dict(config), models=dict(models),
            store_sha256=pinned.get("store_sha256"), snapshot=pinned.get("snapshot"),
            git_commit=SN._git_commit(), bench=bench, seed=seed, budget_usd=budget_usd,
        )

    def finish(self, spent_usd: float) -> None:
        self.finished_at = dt.datetime.now(dt.UTC).isoformat(timespec="seconds")
        self.spent_usd = float(spent_usd)

    def budget(self) -> RunBudget:
        """The run budget the cached backend checks before every live call, starting from what is spent."""
        return RunBudget(cap_usd=self.budget_usd, spent_usd=self.spent_usd)

    # ---- the file

    def as_dict(self) -> dict[str, Any]:
        return asdict(self)

    def write(self, run_dir: Path) -> Path:
        run_dir.mkdir(parents=True, exist_ok=True)
        path = run_dir / "manifest.json"
        _write_atomic(path, json.dumps(self.as_dict(), indent=1, default=str) + "\n")
        return path

    @classmethod
    def read(cls, run_dir: Path) -> "Manifest":
        """The manifest a run wrote. Keys this class does not know are dropped, so a manifest written by a
        newer version still reads; keys it knows and the file lacks take their defaults."""
        raw = json.loads((run_dir / "manifest.json").read_text())
        known = {f.name for f in fields(cls)}
        return cls(**{k: v for k, v in raw.items() if k in known})
