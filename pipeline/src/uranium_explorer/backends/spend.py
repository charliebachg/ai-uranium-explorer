"""The OpenAI backend's view of the spend ledger, kept so its imports and tests do not move.

The ledger itself lives in `uranium_explorer.runtime.spend`, one file for every backend family. Everything here
is that ledger seen through the `openai` family: the same names as before, the same signatures, and the same
`OPENAI_MAX_SPEND_USD` ceiling, now checked alongside the total.
"""

from __future__ import annotations

from pathlib import Path

from ..runtime import spend as _ledger
from ..runtime.spend import BudgetExhausted, Price, RunBudget  # noqa: F401  (re-exported)

FAMILY = "openai"


def ledger_path() -> Path:
    return _ledger.ledger_path()


def spent_usd() -> float:
    """What the OpenAI family has been charged so far."""
    return _ledger.spent_usd(FAMILY, path=ledger_path())


def cap_usd() -> float:
    return _ledger.cap_usd(FAMILY)


def remaining_usd() -> float:
    return cap_usd() - spent_usd()


def check(estimate_usd: float = 0.0) -> None:
    """Refuse before sending, against the OpenAI ceiling and the total."""
    _ledger.check(FAMILY, estimate_usd, path=ledger_path())


def record(model: str, tokens_in: int, tokens_out: int, price: Price, task: str = "") -> float:
    """Append one OpenAI call: measured tokens, dollars as arithmetic over the configured price."""
    return _ledger.record(FAMILY, model, price.usd(tokens_in, tokens_out), tokens_in, tokens_out, task,
                          price=price, path=ledger_path())
