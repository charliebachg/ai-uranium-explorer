"""Conversations in the agent tier, one row per turn, written as they happen.

Nothing here is a source of numbers: the tier CHECK on both tables says so. What is kept is enough to replay
and re-score an answer later — the question, the gated answer, its claims and value ids, the tool calls that
produced them and the gate's objections — which is the raw material of the agent benchmark."""

from __future__ import annotations

import datetime as dt
import json
from pathlib import Path
from typing import Any

from ..store import connect


def _now() -> str:
    return dt.datetime.now(dt.UTC).isoformat(timespec="seconds")


def save_conversation(conversation_id: str, cell_id: str, model: str, backend: str, db_path: Path | None = None) -> None:
    con = connect(db_path)
    try:
        con.execute(
            "insert into agent.conversation (conversation_id, cell_id, model, backend, created_at) "
            "select ?, ?, ?, ?, ? where not exists (select 1 from agent.conversation where conversation_id = ?)",
            [conversation_id, cell_id, model, backend, _now(), conversation_id],
        )
    finally:
        con.close()


def save_turn(conversation_id: str, step: int, turn: dict[str, Any], tool_calls: list[dict[str, Any]],
              values: dict[str, Any], duration_s: float, db_path: Path | None = None) -> str:
    turn_id = f"{conversation_id}:{step}"
    con = connect(db_path)
    try:
        con.execute(
            "insert into agent.conversation_turn (turn_id, conversation_id, step, question, answer_text, published, "
            "problems_json, claims_json, tool_calls_json, values_json, cost_usd, duration_s, created_at) "
            "values (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [turn_id, conversation_id, step, str(turn.get("question") or ""), turn.get("text"),
             bool(turn.get("published")), json.dumps(turn.get("problems") or []), json.dumps(turn.get("claims") or []),
             json.dumps(tool_calls), json.dumps(values), turn.get("cost_usd"), round(duration_s, 3), _now()],
        )
    finally:
        con.close()
    return turn_id


def load_conversation(conversation_id: str, db_path: Path | None = None) -> dict[str, Any] | None:
    con = connect(db_path, read_only=True)
    try:
        head = con.execute("select conversation_id, cell_id, model, backend, created_at from agent.conversation "
                           "where conversation_id = ?", [conversation_id]).fetchone()
        if not head:
            return None
        rows = con.execute(
            "select step, question, answer_text, published, problems_json, claims_json, tool_calls_json, values_json, "
            "cost_usd, duration_s, created_at from agent.conversation_turn where conversation_id = ? order by step",
            [conversation_id]).fetchall()
    finally:
        con.close()
    return {
        "conversation_id": head[0], "cell_id": head[1], "model": head[2], "backend": head[3], "created_at": head[4],
        "turns": [{"step": r[0], "question": r[1], "text": r[2], "published": bool(r[3]),
                   "problems": json.loads(r[4]), "claims": json.loads(r[5]), "tool_calls": json.loads(r[6]),
                   "values": json.loads(r[7]), "cost_usd": r[8], "duration_s": r[9], "created_at": r[10]}
                  for r in rows],
    }


def list_conversations(cell_id: str, db_path: Path | None = None, limit: int = 50) -> list[dict[str, Any]]:
    con = connect(db_path, read_only=True)
    try:
        rows = con.execute(
            "select c.conversation_id, c.model, c.backend, c.created_at, count(t.turn_id), "
            "coalesce(sum(t.cost_usd), 0.0) from agent.conversation c left join agent.conversation_turn t using (conversation_id) "
            "where c.cell_id = ? group by 1, 2, 3, 4 order by c.created_at desc limit ?", [cell_id, limit]).fetchall()
    finally:
        con.close()
    return [{"conversation_id": r[0], "model": r[1], "backend": r[2], "created_at": r[3], "turns": int(r[4]),
             "cost_usd": round(float(r[5]), 4)} for r in rows]
