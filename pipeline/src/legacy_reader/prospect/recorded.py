"""A real conversation, recorded, so the walkthrough does not depend on a server being up.

The chat in the dashboard is live: it calls the model and the answer is gated before it is shown. A guided
tour cannot be, because a demo that stops to wait for a model call — or fails because nobody started
`lr prospect serve` — is a demo that does not get watched.

So this records one. The questions are asked for real, the answers go through the same gate as anything else,
and only the ones that passed are written out with the values they cite. The page labels it as recorded,
because a replayed answer is evidence of what the agent said once, not proof of what it would say now.
"""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any, Callable

from ..paths import PATHS
from .chat import Conversation, ask

#: The three questions the tour asks, in order: what is here, is the score real, and what would settle it.
#: They are the questions a geologist asks in that order, and the third is the one the system is built to
#: answer honestly.
QUESTIONS = (
    "What is actually measured in this cell, and what is only assumed?",
    "How much of this cell's score is explained by the fact that people have already drilled here?",
    "What single observation would most change your reading of this cell?",
)


def record(cell_id: str, backend: Any, model: str = "claude-sonnet-5", effort: str = "medium",
           questions: tuple[str, ...] = QUESTIONS,
           log: Callable[[str], None] = lambda _m: None) -> dict[str, Any]:
    """Ask each question in one conversation, keep what the gate passed, and write it beside the other data."""
    conv = Conversation(cell_id=cell_id)
    turns: list[dict[str, Any]] = []
    for i, question in enumerate(questions, 1):
        log(f"  {i}. {question}")
        turn = ask(conv, question, backend, model=model, effort=effort, log=log)
        if not turn.get("published"):
            log(f"     withheld: {'; '.join(turn.get('problems') or [])}")
        turns.append({
            "question": question,
            "text": turn.get("text"),
            "claims": turn.get("claims") or [],
            "caveats": turn.get("caveats") or [],
            "published": bool(turn.get("published")),
            "problems": turn.get("problems") or [],
            "tools_used": turn.get("tools_used") or [],
        })
        log(f"     {len(turn.get('claims') or [])} claim(s), "
            f"tools {', '.join(turn.get('tools_used') or []) or 'none this turn'}")

    cited = {v for t in turns for c in t["claims"] for v in c.get("value_ids", [])}
    from ..store import connect

    con = connect(read_only=True)
    try:
        centre = con.execute("select lon, lat from derived.cell where cell_id = ?", [cell_id]).fetchone()
    finally:
        con.close()
    return {
        "cell_id": cell_id,
        "lon": centre[0] if centre else None,
        "lat": centre[1] if centre else None,
        "recorded_at": dt.datetime.now(dt.UTC).isoformat(timespec="seconds"),
        "model": model,
        "cost_usd": round(conv.cost_usd, 4),
        "tools_available": sorted({c["tool"] for c in conv.calls}),
        "turns": turns,
        "values": {k: conv.values[k] for k in sorted(cited) if k in conv.values},
    }


def write(payload: dict[str, Any], out: Path | None = None) -> Path:
    path = out or (PATHS.web_data / "prospect" / "recorded_chat.json")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=1) + "\n")
    return path
