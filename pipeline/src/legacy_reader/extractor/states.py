"""The page state machine: one typed record per page, one stage at a time, resumable from disk.

A page moves locate → read → validate → agree → file → done. Each stage writes its own block on the record
and names the next stage; the loop writes the record to `<run_dir>/pages.jsonl` after every stage, so a
run that stops (a budget, a usage limit, a killed process) resumes from the stage each page had reached, and
a stage that already ran is never run twice. A stage that fails leaves the page at that stage with the error
on the record, so a later run can retry it or a reader can see why it stopped.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import asdict, dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any


class Stage(str, Enum):
    LOCATE = "locate"
    READ = "read"
    VALIDATE = "validate"
    AGREE = "agree"
    FILE = "file"
    DONE = "done"

    @property
    def next(self) -> "Stage":
        order = list(Stage)
        return order[min(order.index(self) + 1, len(order) - 1)]


#: the stages in the order a page runs them
ORDER: tuple[Stage, ...] = (Stage.LOCATE, Stage.READ, Stage.VALIDATE, Stage.AGREE, Stage.FILE)

PAGES_FILE = "pages.jsonl"


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


@dataclass
class PageState:
    page_id: str
    file_num: str
    page_no: int
    pdf_sha256: str
    route_class: str
    image_path: str
    stage: Stage = Stage.LOCATE             # the next stage to run
    status: str = "pending"                 # pending | done | failed | stopped
    locate: dict[str, Any] | None = None
    read: dict[str, Any] | None = None
    validate: dict[str, Any] | None = None
    agree: dict[str, Any] | None = None
    file: dict[str, Any] | None = None
    error: str | None = None
    updated_at: str = field(default_factory=_now)

    # ---- transitions

    def finish(self, stage: Stage, block: dict[str, Any]) -> None:
        """Record a stage's result and move to the next one. The block always says when it ran."""
        if stage != self.stage:
            raise ValueError(f"{self.page_id}: cannot finish {stage.value}, the page is at {self.stage.value}")
        setattr(self, stage.value, {**block, "at": _now()})
        self.error = None
        self.stage = stage.next
        self.status = "done" if self.stage is Stage.DONE else "pending"
        self.updated_at = _now()

    def fail(self, stage: Stage, error: BaseException | str) -> None:
        """The stage did not run to the end: the page stays there, with the reason."""
        self.error = f"{type(error).__name__}: {error}" if isinstance(error, BaseException) else str(error)
        self.status = "failed"
        self.updated_at = _now()

    def stop(self, reason: str) -> None:
        """A budget or a usage limit: nothing wrong with the page, the run is what stopped."""
        self.error = reason
        self.status = "stopped"
        self.updated_at = _now()

    @property
    def done(self) -> bool:
        return self.stage is Stage.DONE

    def as_dict(self) -> dict[str, Any]:
        d = asdict(self)
        d["stage"] = self.stage.value
        return d

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "PageState":
        raw = dict(d)
        raw["stage"] = Stage(raw.get("stage", "locate"))
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})


# --------------------------------------------------------------------------------- the file

def read_states(run_dir: Path) -> dict[str, PageState]:
    """The last record of every page in a run directory: the file is append-only, later lines win."""
    path = run_dir / PAGES_FILE
    if not path.is_file():
        return {}
    out: dict[str, PageState] = {}
    for line in path.read_text().splitlines():
        if line.strip():
            st = PageState.from_dict(json.loads(line))
            out[st.page_id] = st
    return out


def append_state(run_dir: Path, state: PageState) -> None:
    run_dir.mkdir(parents=True, exist_ok=True)
    with (run_dir / PAGES_FILE).open("a") as f:
        f.write(json.dumps(state.as_dict(), separators=(",", ":"), default=str) + "\n")
        f.flush()
