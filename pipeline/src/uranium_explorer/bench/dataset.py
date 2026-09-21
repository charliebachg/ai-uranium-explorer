"""Where the benchmark dataset lives, and the one rule about it: it is not in the repository.

The dataset is the small, citable part of a benchmark: the analyst track's cell list, held-out list, answer
key, manifest and baseline table, and the interface track's tier files with their manifest. It is unfinished,
so it is not committed. On the machine that builds it, `pipeline/knowledge/bench/` is a symlink to where it
lives; anywhere else, `UE_BENCH_KNOWLEDGE_DIR` names the directory. The built packs, cards and passages under
`data/bench/` are a different tree, gitignored on their own account, and are not affected.

A clone without the dataset gets one plain line naming the command that builds what it lacks, never a
traceback: the loaders raise `FileNotFoundError` with that line, and `ue bench` prints it and exits 2.
"""

from __future__ import annotations

import os
from pathlib import Path

from ..paths import PATHS

#: names the dataset directory; unset, it is the in-repo path (a symlink on the machine that builds it)
ENV = "UE_BENCH_KNOWLEDGE_DIR"


def knowledge_dir() -> Path:
    """The dataset directory: `UE_BENCH_KNOWLEDGE_DIR` when set, else `pipeline/knowledge/bench/`.

    Read on every call, not at import, so a test or a shell can point it elsewhere without reloading."""
    env = os.environ.get(ENV)
    return Path(env).expanduser() if env else PATHS.pipeline / "knowledge" / "bench"


def hint(command: str) -> str:
    """How to get a benchmark that is not on disk: build it (the command reads the store), or name the
    directory that already holds the dataset."""
    return f"run `{command}`, or point {ENV} at a directory that holds the dataset"


__all__ = ["ENV", "hint", "knowledge_dir"]
