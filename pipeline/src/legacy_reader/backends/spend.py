"""A hard ceiling on what the demo may spend, enforced before a call is sent rather than regretted after.

The budget for this backend is small and real. A per-call cap does not protect it: twenty cheap calls empty a
purse just as well as one expensive one, and the usual failure is a retry loop nobody is watching. So the
ledger is cumulative, it lives on disk, and it survives a restart — a fresh process does not get a fresh
budget.

Two things are kept apart on purpose. **Tokens are measured**: the API reports them and they are recorded as
returned. **Dollars are arithmetic** over a price the operator configured, because a price this file guessed
would be a fabricated number, and this project does not print those.
"""

from __future__ import annotations

import datetime as dt
import json
import os
from dataclasses import dataclass
from pathlib import Path

from ..paths import PATHS


class BudgetExhausted(RuntimeError):
    """Raised before a call is sent, when sending it could cross the ceiling."""


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


def ledger_path() -> Path:
    return PATHS.data / "openai_spend.jsonl"


def spent_usd() -> float:
    """Everything charged to this ledger so far. A missing or damaged line counts as unknown, not as zero."""
    path = ledger_path()
    if not path.exists():
        return 0.0
    total = 0.0
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            total += float(json.loads(line).get("usd") or 0.0)
        except (json.JSONDecodeError, TypeError, ValueError):
            # a line we cannot read is a call we cannot account for; refuse to treat it as free
            total += float("nan")
    return total


def cap_usd() -> float:
    return float(os.environ.get("OPENAI_MAX_SPEND_USD", "2.00"))


def remaining_usd() -> float:
    return cap_usd() - spent_usd()


def check(estimate_usd: float = 0.0) -> None:
    """Refuse before sending. `estimate_usd` is the worst case for the call about to be made."""
    spent = spent_usd()
    if spent != spent:  # NaN: an unreadable ledger line
        raise BudgetExhausted(f"{ledger_path()} has a line this code cannot read; refusing to spend blind")
    cap = cap_usd()
    if spent + estimate_usd > cap:
        raise BudgetExhausted(
            f"this call could take the total to ${spent + estimate_usd:.4f} against a ${cap:.2f} ceiling "
            f"(${spent:.4f} already spent). Raise OPENAI_MAX_SPEND_USD in .env, or stop here."
        )


def record(model: str, tokens_in: int, tokens_out: int, price: Price, task: str = "") -> float:
    """Append one call to the ledger and return what it was charged."""
    usd = price.usd(tokens_in, tokens_out)
    path = ledger_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a") as fh:
        fh.write(json.dumps({
            "at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
            "task": task,
            "model": model,
            "tokens_in": tokens_in,
            "tokens_out": tokens_out,
            "per_mtok_in": price.per_mtok_in,
            "per_mtok_out": price.per_mtok_out,
            # unrounded: rounding down a fraction of a cent on every call is a slow leak in the ceiling
            "usd": usd,
        }) + "\n")
    return usd
