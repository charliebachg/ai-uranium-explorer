import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { BBox, BenchBlock, DatumGrid, Val, ValRegistry, ValueId } from "@/data/contract";

const lineage = {
  file_num: "64L04-0075",
  file_sha256: "a".repeat(64),
  page: 1,
  bbox: [0.1, 0.2, 0.3, 0.25],
  quote: "ELEVATION:   434.38",
  quote_located: true,
  model: "claude-sonnet-5",
  prompt_version: "p1",
  run_id: "r1",
  extracted_at: "2026-09-18T00:00:00Z",
  validators: [],
};

describe("contract", () => {
  it("accepts namespaced value ids and rejects bare ones", () => {
    expect(ValueId.safeParse("x:64L04-0075:9f2c1a7b").success).toBe(true);
    expect(ValueId.safeParse("m:compilation_collars").success).toBe(true);
    expect(ValueId.safeParse("434.38").success).toBe(false);
    expect(ValueId.safeParse("q:thing").success).toBe(false);
  });

  it("requires lineage on extracted values", () => {
    const base = {
      id: "x:64L04-0075:1",
      kind: "extracted",
      as_printed: "434.38",
      value: 434.38,
      unit_as_printed: null,
    };
    expect(Val.safeParse(base).success).toBe(false);
    expect(Val.safeParse({ ...base, lineage }).success).toBe(true);
  });

  it("requires a derivation on derived values and a formatter on non-extracted values", () => {
    const d = {
      id: "d:64L04-0075:shift",
      kind: "derived",
      as_printed: null,
      value: 34.23,
      unit_as_printed: null,
    };
    expect(Val.safeParse({ ...d, fmt: "m1" }).success).toBe(false);
    expect(
      Val.safeParse({ ...d, derivation: { op: "ntv2_shift", inputs: [], tool: "pyproj" } }).success,
    ).toBe(false);
    expect(
      Val.safeParse({ ...d, fmt: "m1", derivation: { op: "ntv2_shift", inputs: [], tool: "pyproj" } })
        .success,
    ).toBe(true);
  });

  it("rejects registry keys that differ from the value id", () => {
    const v = {
      id: "m:files_read",
      kind: "stat",
      as_printed: null,
      value: 12,
      unit_as_printed: null,
      fmt: "int",
    };
    expect(ValRegistry.safeParse({ "m:files_read": v }).success).toBe(true);
    expect(ValRegistry.safeParse({ "m:other": v }).success).toBe(false);
  });

  it("keeps boxes normalised", () => {
    expect(BBox.safeParse([0, 0, 1, 1]).success).toBe(true);
    expect(BBox.safeParse([10, 20, 300, 40]).success).toBe(false);
    expect(BBox.safeParse([0.5, 0.5, 0.4, 0.6]).success).toBe(false);
  });

  it("validates the real datum grid exported by the pipeline", () => {
    const path = resolve(__dirname, "../../public/data/context/datum_grid.json");
    const grid = DatumGrid.parse(JSON.parse(readFileSync(path, "utf8")));
    expect(grid.nlat * grid.nlon).toBe(grid.de_m.length);
    const east = grid.checks.find((c) => c.label.startsWith("Eastern"));
    expect(east && Math.abs(east.computed_m - 34.2)).toBeLessThan(0.3);
  });

  it("accepts the analyst benchmark block as the pipeline writes it and refuses a row of the wrong kind", () => {
    const arm = {
      name: "v0",
      kind: "arm",
      model: "claude-opus-5",
      n: "c:bench:v1:v0:n",
      n_pos: "c:bench:v1:v0:n_pos",
      n_neg: null,
      run_id: "20260920T061420Z-bench",
      mlflow_run_id: null,
      metrics: { f1: "c:bench:v1:v0:f1", pr_auc: "c:bench:v1:v0:pr_auc" },
      ci: { f1: ["c:bench:v1:v0:f1.lo", "c:bench:v1:v0:f1.hi"] },
      extra: { cost_usd_per_cell: "c:bench:v1:v0:cost_usd_per_cell" },
      strata: { deposit: { n: "c:bench:v1:v0:deposit:n", accuracy: "c:bench:v1:v0:deposit:accuracy" } },
    };
    // a baseline names no run and may carry nothing beyond its metrics
    const baseline = {
      name: "random_expected",
      kind: "baseline",
      model: "random",
      n: "c:bench:v1:random_expected:n",
      n_pos: null,
      n_neg: null,
      run_id: null,
      mlflow_run_id: null,
      note: "analytic expectation; no interval",
      metrics: { f1: "c:bench:v1:random_expected:f1" },
    };
    const block = {
      version: "v1",
      manifest_sha256: "b".repeat(64),
      computed_at: null,
      versions: [],
      rows: [arm, baseline],
    };
    expect(BenchBlock.safeParse(block).success).toBe(true);
    expect(BenchBlock.safeParse({ ...block, rows: [{ ...arm, kind: "model" }] }).success).toBe(false);
    expect(BenchBlock.safeParse({ ...block, rows: [{ ...arm, metrics: { f1: "0.65" } }] }).success).toBe(
      false,
    );
    expect(
      BenchBlock.safeParse({ ...block, rows: [{ ...arm, ci: { f1: ["c:bench:v1:v0:f1.lo"] } }] }).success,
    ).toBe(false);
  });
});
