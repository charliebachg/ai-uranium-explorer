"""The sensitivity over the criteria table: deterministic, from ids to ids, on a fake criteria world."""

from __future__ import annotations

from legacy_reader.interface.sensitivity import sensitivity
from legacy_reader.prospect import tools as T
from legacy_reader.prospect.memo import check_claims

CELL = "0100_0100"


def breakdown(rows: list[tuple[str, float, float | None]], status: str = "published") -> T.ToolResult:
    """A criteria breakdown in the tool's own shape: (criterion, weight, membership or None for unknown)."""
    out = T.ToolResult("criteria_breakdown", {"cell_id": CELL})
    for key, w, m in rows:
        row = {"criterion": key, "title": key, "weight": w, "weight_id": f"c:crit:{CELL}:{key}:weight",
               "status": status, "state": "unknown" if m is None else ("met" if m >= 0.5 else "not met")}
        if m is not None:
            row["membership"] = m
        out.rows.append(row)
    return out


def test_the_heaviest_unknown_moves_the_score_most_and_every_figure_has_an_id() -> None:
    # known: conductor 2.0 x 1.0, host 1.0 x 0.0 -> score 2/3; unknown: fault 1.0, alteration 0.5
    res = sensitivity(CELL, breakdown([("conductor", 2.0, 1.0), ("host", 1.0, 0.0), ("fault", 1.0, None),
                                       ("alteration", 0.5, None)]), min_known_weight=0.5)
    now = res.rows[0]
    assert now["scorable"] is True and now["score_now"] == round(2 / 3, 4)
    assert now["known_share"] == round(3.0 / 4.5, 4) and now["n_unknown"] == 2
    ranked = [r for r in res.rows if r["row"] == "if_measured"]
    assert [r["criterion"] for r in ranked] == ["fault", "alteration"]
    fault = ranked[0]
    assert fault["rank"] == 1
    assert fault["score_if_met"] == round(3.0 / 4.0, 4) and fault["score_if_not_met"] == round(2.0 / 4.0, 4)
    assert fault["delta_if_met"] == round(3.0 / 4.0 - 2.0 / 3.0, 4)
    assert fault["known_share_after"] == round(4.0 / 4.5, 4)
    # every figure on a row is a value with an id in the registry, under the cell's sensitivity namespace
    for row in res.rows:
        for key in ("score_now", "score_if_met", "score_if_not_met", "delta_if_met", "known_share", "known_share_after"):
            if key in row:
                vid = row[f"{key}_id"]
                assert vid.startswith(f"c:sens:{CELL}:") and res.values[vid]["value"] == row[key]
    # a claim quoting one passes the gate by citing its id, and one that does not is refused
    ok = check_claims([{"text": f"if the fault were measured and met the score would be {fault['score_if_met']}",
                        "value_ids": [fault["score_if_met_id"]]}], res.values)
    assert ok == []
    assert check_claims([{"text": "the score would move to 0.81", "value_ids": [fault["score_if_met_id"]]}], res.values)


def test_the_same_world_gives_the_same_ranking_whatever_the_row_order() -> None:
    rows = [("a", 1.0, 1.0), ("b", 1.0, None), ("c", 1.0, None), ("d", 2.0, None)]
    one = sensitivity(CELL, breakdown(rows), 0.5)
    two = sensitivity(CELL, breakdown(list(reversed(rows))), 0.5)
    assert one.rows == two.rows and one.values == two.values
    assert [r["criterion"] for r in one.rows[1:]] == ["d", "b", "c"], "by weight, then by key on a tie"


def test_a_cell_too_little_known_to_score_ranks_what_would_make_it_scorable() -> None:
    res = sensitivity(CELL, breakdown([("a", 1.0, 1.0), ("b", 2.0, None), ("c", 0.5, None)]), min_known_weight=0.6)
    now = res.rows[0]
    assert now["scorable"] is False and now["score_now"] is None and "score_now_id" not in now
    ranked = {r["criterion"]: r for r in res.rows[1:]}
    assert ranked["b"]["scorable_after"] is True and ranked["b"]["rank"] == 1
    assert ranked["c"]["scorable_after"] is False and "delta_if_met" not in ranked["c"]
    assert "scorable" in ranked["b"]["note"] and "still leave" in ranked["c"]["note"]


def test_folklore_is_named_but_never_ranked() -> None:
    res = sensitivity(CELL, breakdown([("a", 1.0, 1.0), ("rumour", 0.0, None)], status="folklore"), 0.5)
    assert [r["row"] for r in res.rows] == ["now"], "a weight-zero criterion cannot move the score"
    assert "Every counted criterion is measured" in res.note
