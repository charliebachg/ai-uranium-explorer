"""Analyst v2: four family readers, each gated with one retry, then one ranking call under v0's gate."""

from __future__ import annotations

import re
from dataclasses import replace

import pytest

from uranium_explorer.analyst import arms as A
from uranium_explorer.analyst import families as FAM
from uranium_explorer.analyst import run as RUN
from uranium_explorer.analyst import score as SC
from uranium_explorer.backends.base import ExtractionRequest, ExtractionResponse
from fake_bench import good_answer, install_runtime, make_bench, make_pack

GEO_ID, BOULDER_ID, SED_ID = "b:b01:ev:s0:u_ppm", "b:b01:ev:b0:cps", "b:b01:cell:sed_u_max_ppm"


def rich_pack() -> dict:
    """The fake pack plus one record for each family's evidence tool and one geochemistry feature."""
    pack = make_pack("b01")
    rows = {"evidence_geochem": ("lake sediment", "u_ppm", 41.0, GEO_ID),
            "evidence_boulders": ("boulder", "cps", 9000.0, BOULDER_ID),
            "evidence_structure": ("lineament", "dist_m", 133.0, "b:b01:ev:f0:dist_m"),
            "evidence_bedrock": ("bedrock unit", "share_of_area", 1.0, "b:b01:ev:u0:share_of_area"),
            "region": ("cover", "dist_to_cover_edge_m", 39955.0, "b:b01:ev:cov:dist_to_cover_edge_m")}
    for tool, (kind, key, value, vid) in rows.items():
        pack["tools"][tool] = {"note": f"{tool} note", "rows": [{"kind": kind, key: value, f"{key}_id": vid}]}
        pack["values"][vid] = {"id": vid, "value": value}
    pack["tools"]["cell_features"]["rows"].append({"feature": "sed_u_max_ppm", "value": 41.0, "value_id": SED_ID})
    pack["values"][SED_ID] = {"id": SED_ID, "value": 41.0}
    return pack


def d2_arm() -> A.ArmConfig:
    return A.load_arm("d2")


class ReadingBackend:
    """Readers answer by family, the ranking call with v0's good answer. `fail` maps a family to how many of its
    first attempts cite a number from nowhere."""

    family = "claude_cli"
    _READ = re.compile(r"^Cell (\S+)\. Read (\w+):")

    def __init__(self, fail: dict[str, int] | None = None, cost: float = 0.05):
        self.fail = dict(fail or {})
        self.cost = cost
        self.requests: list[ExtractionRequest] = []

    def reading(self, family: str, bench_id: str, shown: str | None = None) -> dict:
        """The geochemistry reader cites the sample when its share holds it, as an honest reader would."""
        vid = f"b:{bench_id}:ev:s0:u_ppm"
        if family == "geochemistry" and (shown is None or vid in shown):
            claims = [{"text": "The nearest sample carries 41 ppm uranium.", "value_ids": [vid]}]
        else:
            claims = []
        return {"assessment": "for" if claims else "unknown", "strength": 0.7 if claims else 0.5, "claims": claims,
                "unknowns": [] if claims else ["too little here"], "summary": "Read as instructed."}

    def call(self, req: ExtractionRequest) -> ExtractionResponse:
        self.requests.append(req)
        if req.task == FAM.TASK_STAGE:
            bench_id, family = self._READ.match(req.user_prompt).groups()
            out = self.reading(family, bench_id, req.stage_files[0][0].read_text())
            if self.fail.get(family, 0) > 0:
                self.fail[family] -= 1
                out = {**out, "claims": [{"text": "Grades reached 2.4% U3O8.", "value_ids": []}]}
        else:
            bench_id = re.match(r"^Cell (\S+)\.", req.user_prompt).group(1)
            out = good_answer(bench_id)
        return ExtractionResponse(structured=out, envelope={}, backend="scripted", backend_version="0",
                                  model_requested=req.model, model_resolved=req.model, num_turns=1,
                                  duration_s=1.0, usage={}, cost_usd=self.cost,
                                  cache_key=req.cache_key(self.family))


def test_a_readers_share_is_its_own_tools_rows_and_the_values_they_cite() -> None:
    geo = next(f for f in FAM.FAMILIES if f.name == "geochemistry")
    part = FAM.share(rich_pack(), geo)
    assert set(part["tools"]) == {"evidence_geochem", "cell_features", "coverage", "criteria_breakdown"}
    assert {r["feature"] for r in part["tools"]["cell_features"]["rows"]} == {"sed_u_max_ppm", "water_u_max_ppm"}
    assert [r["criterion"] for r in part["tools"]["criteria_breakdown"]["rows"]] == ["lake_water_uranium"]
    assert set(part["values"]) == {GEO_ID, SED_ID}, "only what its own rows cite"
    dispersal = FAM.share(rich_pack(), next(f for f in FAM.FAMILIES if f.name == "dispersal"))
    assert set(dispersal["values"]) == {BOULDER_ID} and "evidence_geochem" not in dispersal["tools"]


def test_every_feature_and_criterion_belongs_to_one_family_at_most() -> None:
    feats = [k for f in FAM.FAMILIES for k in f.features]
    crits = [k for f in FAM.FAMILIES for k in f.criteria]
    assert len(feats) == len(set(feats)) and len(crits) == len(set(crits))
    assert not any(k.startswith("domain_") for k in feats), "the domain one-hots name ground"


def test_a_reading_is_gated_like_an_answer() -> None:
    geo = next(f for f in FAM.FAMILIES if f.name == "geochemistry")
    part = FAM.share(rich_pack(), geo)
    good = ReadingBackend().reading("geochemistry", "b01")
    assert FAM.gate_reading(good, part) == []
    bad = {**good, "claims": [{"text": "Grades reached 2.4% U3O8.", "value_ids": []}]}
    assert FAM.gate_reading(bad, part)
    assert any("assessment" in p for p in FAM.gate_reading({**good, "assessment": "maybe"}, part))
    outside = {**good, "claims": [{"text": "A boulder reads 9000 cps.", "value_ids": [BOULDER_ID]}]}
    assert FAM.gate_reading(outside, part), "a value from another family's share cannot be cited"


def test_a_cell_is_four_readings_then_one_ranking_call_that_sees_them(tmp_path) -> None:
    backend = ReadingBackend()
    row = FAM.run_cell(backend, rich_pack(), None, d2_arm(), stage=tmp_path)
    assert [r.task for r in backend.requests] == [FAM.TASK_STAGE] * 4 + [FAM.TASK_RANK]
    assert row["published"] and row["cost_usd"] == pytest.approx(0.25)
    assert set(row["readings"]) == {f.name for f in FAM.FAMILIES}
    geo = row["readings"]["geochemistry"]
    assert (geo["assessment"], geo["strength"], geo["published"], geo["attempts"]) == ("for", 0.7, True, 1)
    rank = backend.requests[-1]
    assert [name for _, name in rank.stage_files] == ["pack.md", FAM.READINGS_FILE]
    readings = (tmp_path / FAM.READINGS_FILE).read_text()
    assert "## geochemistry: for, strength 0.7" in readings and GEO_ID in readings
    assert "readings.md" in rank.user_prompt and rank.system_prompt.endswith(FAM.V0.EVIDENCE_GUIDE)
    # each reader saw only its share: the dispersal reader's pack names no geochemistry value
    assert GEO_ID not in (tmp_path / "dispersal" / "pack.md").read_text()


def test_a_refused_reading_is_asked_again_once_told_why_and_left_out_if_refused_twice(tmp_path) -> None:
    backend = ReadingBackend(fail={"geochemistry": 1, "structure": 2})
    row = FAM.run_cell(backend, rich_pack(), None, d2_arm(), stage=tmp_path)
    assert len(backend.requests) == 7, "two readers retried once each, then the ranking call"
    geo, struct = row["readings"]["geochemistry"], row["readings"]["structure"]
    assert geo["attempts"] == 2 and geo["published"] and not geo["problems"]
    assert struct["attempts"] == 2 and not struct["published"] and struct["problems"]
    retry = [r for r in backend.requests if "Read geochemistry" in r.user_prompt][1]
    assert "previous answer was refused" in retry.user_prompt
    assert "## structure: refused by the check, not usable" in (tmp_path / FAM.READINGS_FILE).read_text()
    assert row["published"], "the ranking call is gated on its own"


def test_the_reading_prompts_name_no_place_and_differ_only_in_the_focus() -> None:
    texts = [FAM.stage_system(f, d2_arm().switches) for f in FAM.FAMILIES]
    assert len(set(texts)) == 4 and not any(FAM.V0.place_names_in(t) for t in texts)
    assert set(FAM.prompt_hashes(d2_arm().switches)) == {*(f"reading/{f.name}" for f in FAM.FAMILIES), "rank"}


def test_a_v2_arm_runs_through_the_harness_and_scores_like_v0(monkeypatch, tmp_path) -> None:
    rt = install_runtime(monkeypatch, tmp_path)
    bench = make_bench(tmp_path)
    # the fake benchmark was built without the later switches, so this v2 arm leaves them off
    arm = replace(d2_arm(), switches=A.load_arm("v0").switches)
    backend = ReadingBackend()
    s = RUN.run_arm(bench.version, arm, lambda _arm: backend, budget_usd=5.0, log=lambda *_: None, workers=1,
                    track=False, cache_root=rt.cache, boot=5)
    assert s["done"] == 5 and s["failed"] == 0
    rows = SC.read_cells(rt.runs / s["run_id"])
    assert all(len(r["readings"]) == 4 and r["cost_usd"] == pytest.approx(0.25) for r in rows.values())
    assert s["score"]["n"] == 4


def test_an_arm_asking_for_the_evidence_is_refused_on_a_benchmark_built_without_it(monkeypatch, tmp_path) -> None:
    rt = install_runtime(monkeypatch, tmp_path)
    bench = make_bench(tmp_path)
    with pytest.raises(ValueError, match="evidence, which benchmark"):
        RUN.run_arm(bench.version, d2_arm(), lambda _arm: ReadingBackend(), budget_usd=5.0, log=lambda *_: None,
                    track=False, cache_root=rt.cache)
