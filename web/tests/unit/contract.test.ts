import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { BBox, DatumGrid, Val, ValRegistry, ValueId } from "@/data/contract";

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
});
