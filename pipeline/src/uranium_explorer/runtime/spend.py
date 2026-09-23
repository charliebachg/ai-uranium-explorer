"""One ledger for everything the pipeline spends, checked before a call is sent rather than regretted after.

Every backend family writes to one file, so one command answers "what has every call cost". The CLI family
behind `claude -p` is written too, at the envelope's `total_cost_usd`: a memo panel that loops on a tool error
burns subscription quota as fast as it would burn money, and the list-price figure is how that shows.

But only a **billed** family is money. The CLI runs on a flat subscription, so its list-price figures are kept
out of the cumulative ceilings: counting them let a benchmark run on the subscription (about $850 at list
price) lock the pay-per-token chat out of a $600 ceiling it had barely touched. The ceilings, both cumulative
and on disk so a fresh process does not get a fresh budget:

* the **total** (`UE_MAX_SPEND_USD`, default 300) across every billed family, and
* a **family** ceiling where a family has its own — the OpenAI key keeps `OPENAI_MAX_SPEND_USD` (default 2.00).

And one **run budget**, in memory, handed to the cached backend by whoever started the run: it stops one
runaway loop long before a ceiling has to, and it is the bound on a subscription run, beside the
subscription's own usage limit (the CLI stops with exit 75 when that is reached).

What is written is what was measured. The CLI family's dollars are the envelope's `total_cost_usd`, recorded
as returned. The OpenAI family's tokens are the API's and its dollars are arithmetic over a price the operator
configured, because a price this file guessed would be a fabricated number, and this project does not print
those.
"""

from __future__ import annotations

import datetime as dt
import json
import os
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..paths import PATHS

#: backend families that have a ceiling of their own, as (environment variable, default)
FAMILY_CAPS: dict[str, tuple[str, float]] = {"openai": ("OPENAI_MAX_SPEND_USD", 2.00),
                                              "openrouter": ("OPENROUTER_MAX_SPEND_USD", 25.00)}
TOTAL_CAP: tuple[str, float] = ("UE_MAX_SPEND_USD", 300.0)
#: families paid by a flat subscription, not per token: recorded, never counted against the cumulative ceilings
SUBSCRIPTION_FAMILIES: frozenset[str] = frozenset({"claude_cli"})

_append_lock = threading.Lock()


class BudgetExhausted(RuntimeError):
    """Raised before a call is sent, when sending it could cross a ceiling or the run's budget."""


@dataclass(frozen=True)
class Price:
    """What the operator says a million tokens cost. Not discovered, not guessed: read from the environment."""

    per_mtok_in: float
    per_mtok_out: float

    def usd(self, tokens_in: int, tokens_out: int) -> float:
        return (tokens_in / 1e6) * self.per_mtok_in + (tokens_out / 1e6) * self.per_mtok_out

    @classmethod
    def from_env(cls) -> "Price":
        return cls(
            per_mtok_in=float(os.environ.get("OPENAI_PRICE_IN_PER_MTOK", "0.25")),
            per_mtok_out=float(os.environ.get("OPENAI_PRICE_OUT_PER_MTOK", "2.00")),
        )


@dataclass
class RunBudget:
    """What one run may spend. `cap_usd` None means the run is bounded by the ceilings alone."""

    cap_usd: float | None
    spent_usd: float = 0.0

    def remaining(self) -> float | None:
        return None if self.cap_usd is None else self.cap_usd - self.spent_usd


# ---------------------------------------------------------------- the file


def ledger_path() -> Path:
    return PATHS.data / "spend.jsonl"


def legacy_ledger_path() -> Path:
    """The OpenAI-only ledger this one replaced. Imported once, the first time the new ledger is read."""
    return PATHS.data / "openai_spend.jsonl"


def _migrate(path: Path) -> None:
    """Bring the old OpenAI ledger's lines across, tagged with their family, when the new ledger does not exist
    yet. The old file is left where it was: it is history, and a second import cannot happen while the new
    file exists."""
    old = legacy_ledger_path()
    if path.exists() or not old.exists():
        return
    lines = [ln for ln in old.read_text().splitlines() if ln.strip()]
    out = []
    for ln in lines:
        try:
            row = json.loads(ln)
        except json.JSONDecodeError:
            out.append(ln)   # carried across unreadable, so it still refuses to count as free
            continue
        out.append(json.dumps({"family": "openai", **row}))
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text("".join(ln + "\n" for ln in out))


def _rows(path: Path | None = None) -> list[dict[str, Any] | None]:
    """Every line of the ledger; None stands for a line this code cannot read."""
    if path is None:
        path = ledger_path()
        _migrate(path)
    if not path.exists():
        return []
    rows: list[dict[str, Any] | None] = []
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            rows.append(json.loads(line))
        except json.JSONDecodeError:
            rows.append(None)
    return rows


# ---------------------------------------------------------------- reading


def spent_usd(family: str | None = None, *, path: Path | None = None) -> float:
    """Everything charged so far, to one family or to all. A line we cannot read is a call we cannot account
    for, so it makes the total NaN rather than zero, and `check` refuses to spend blind."""
    total = 0.0
    for row in _rows(path):
        if row is None:
            total += float("nan")
            continue
        if family is not None and row.get("family") != family:
            continue
        try:
            total += float(row.get("usd") or 0.0)
        except (TypeError, ValueError):
            total += float("nan")
    return total


def billed_usd(*, path: Path | None = None) -> float:
    """What the billed families have been charged: every line but the subscription's. An unreadable line is NaN
    here too, since nobody can say which purse it belonged to."""
    total = 0.0
    for row in _rows(path):
        if row is None:
            total += float("nan")
            continue
        if row.get("family") in SUBSCRIPTION_FAMILIES:
            continue
        try:
            total += float(row.get("usd") or 0.0)
        except (TypeError, ValueError):
            total += float("nan")
    return total


def by_family(*, path: Path | None = None) -> dict[str, float]:
    """Spend per family, for the `spend show` command. An unreadable line lands under "unreadable" as NaN."""
    out: dict[str, float] = {}
    for row in _rows(path):
        if row is None:
            out["unreadable"] = float("nan")
            continue
        fam = str(row.get("family") or "unknown")
        try:
            out[fam] = out.get(fam, 0.0) + float(row.get("usd") or 0.0)
        except (TypeError, ValueError):
            out[fam] = float("nan")
    return out


def cap_usd(family: str | None = None) -> float:
    """The ceiling that applies: the total for None, a family's own where it has one, else the total (every
    family is bounded by the total anyway)."""
    env, default = FAMILY_CAPS.get(family or "", TOTAL_CAP)
    return float(os.environ.get(env, str(default)))


def check(family: str, estimate_usd: float = 0.0, run_budget: RunBudget | None = None, *,
          path: Path | None = None) -> None:
    """Refuse before sending. `estimate_usd` is the worst case for the call about to be made.

    Three ceilings, in the order they are cheapest to explain: the run's own budget, the total across every
    billed family, and the family's own where it has one. A subscription family meets the run budget only: its
    calls add nothing to what is billed. Exactly reaching a ceiling is allowed; crossing it is not. A run budget
    is anything with `cap_usd` and `spent_usd` (a test's stand-in as much as `RunBudget`)."""
    if run_budget is not None and run_budget.cap_usd is not None:
        cap, spent = float(run_budget.cap_usd), float(run_budget.spent_usd)
        if spent + estimate_usd > cap:
            raise BudgetExhausted(
                f"this call could take the run to ${spent + estimate_usd:.4f} against its ${cap:.2f} budget "
                f"(${spent:.4f} already spent)."
            )
    total = billed_usd(path=path)
    if total != total:  # NaN: an unreadable ledger line
        raise BudgetExhausted(f"{path or ledger_path()} has a line this code cannot read; refusing to spend blind")
    if family in SUBSCRIPTION_FAMILIES:
        return
    cap = cap_usd(None)
    if total + estimate_usd > cap:
        raise BudgetExhausted(
            f"this call could take the billed total to ${total + estimate_usd:.4f} against a ${cap:.2f} ceiling "
            f"(${total:.4f} already billed; subscription calls are recorded, not counted). Raise {TOTAL_CAP[0]} "
            f"in .env, or stop here."
        )
    if family in FAMILY_CAPS:
        fam_spent = spent_usd(family, path=path)
        fam_cap = cap_usd(family)
        if fam_spent + estimate_usd > fam_cap:
            raise BudgetExhausted(
                f"this call could take the {family} total to ${fam_spent + estimate_usd:.4f} against a "
                f"${fam_cap:.2f} ceiling (${fam_spent:.4f} already spent). Raise {FAMILY_CAPS[family][0]} in "
                f".env, or stop here."
            )


# ---------------------------------------------------------------- writing


def record(family: str, model: str, usd: float, tokens_in: int = 0, tokens_out: int = 0, task: str = "", *,
           price: Price | None = None, path: Path | None = None) -> float:
    """Append one call to the ledger and return what it was charged.

    `usd` is unrounded on purpose: rounding down a fraction of a cent on every call is a slow leak in the
    ceiling. `price` is written beside the tokens when the dollars were computed from one, so the line shows
    its own arithmetic."""
    line: dict[str, Any] = {
        "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "family": family,
        "task": task,
        "model": model,
        "tokens_in": int(tokens_in or 0),
        "tokens_out": int(tokens_out or 0),
    }
    if price is not None:
        line["per_mtok_in"] = price.per_mtok_in
        line["per_mtok_out"] = price.per_mtok_out
    line["usd"] = float(usd or 0.0)
    target = path or ledger_path()
    with _append_lock:
        target.parent.mkdir(parents=True, exist_ok=True)
        with target.open("a") as fh:
            fh.write(json.dumps(line) + "\n")
    return line["usd"]
