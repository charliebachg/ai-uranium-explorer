import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { DatumGrid } from "@/data/contract";
import { compassPoint, shiftAt, utmZone } from "@/map/geo/datumGrid";

const grid = DatumGrid.parse(
  JSON.parse(readFileSync(resolve(__dirname, "../../public/data/context/datum_grid.json"), "utf8")),
);

describe("datum grid interpolation (instrument)", () => {
  it("reproduces grid nodes and the reference shifts", () => {
    for (const c of grid.checks) {
      const s = shiftAt(grid, c.lon, c.lat);
      expect(s).not.toBeNull();
      expect(Math.abs((s?.dist ?? 0) - c.computed_m)).toBeLessThan(0.1);
    }
  });

  it("points west-northwest across the basin and returns null outside the grid", () => {
    const s = shiftAt(grid, -105.0, 58.0);
    expect(compassPoint(s?.bearing ?? 0)).toMatch(/^W|^NW|^WNW/);
    expect(shiftAt(grid, -120, 50)).toBeNull();
  });

  it("computes UTM zones", () => {
    expect(utmZone(-109.5)).toBe(12);
    expect(utmZone(-105)).toBe(13);
  });
});
