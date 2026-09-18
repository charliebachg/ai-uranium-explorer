import { readFileSync } from "node:fs";
import { resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { DatumGrid } from "@/data/contract";
import { arrowField, arrowScaleForZoom } from "@/map/layers/datum";

const grid = DatumGrid.parse(
  JSON.parse(readFileSync(resolve(__dirname, "../../public/data/context/datum_grid.json"), "utf8")),
);

describe("shift-arrow field", () => {
  it("subsamples nodes and caps the exaggeration it reports", () => {
    const regional = arrowScaleForZoom(7.6, 427);
    expect(regional.step).toBe(4);
    expect(regional.exaggeration).toBeGreaterThan(50);
    expect(regional.exaggeration).toBeLessThanOrEqual(200);
    const close = arrowScaleForZoom(13, 13);
    expect(close.step).toBe(1);
    expect(close.exaggeration).toBeLessThan(20);
  });

  it("points arrows west-northwest, the direction of the real NAD27 to NAD83 shift", () => {
    const fc = arrowField(grid, 8, 1);
    expect(fc.features.length).toBeGreaterThan(20);
    for (const f of fc.features.slice(0, 20)) {
      const coords = (f.geometry as GeoJSON.LineString).coordinates;
      // the drawn path is head, tip, tail, tip, head: the tail is the grid node
      const tip = coords[1] as [number, number];
      const tail = coords[2] as [number, number];
      expect(tip[0]).toBeLessThan(tail[0]); // west
      expect(tip[1]).toBeGreaterThan(tail[1]); // and slightly north
    }
  });

  it("scales length with the shift magnitude", () => {
    const fc = arrowField(grid, 8, 10);
    const len = (f: GeoJSON.Feature) => {
      const c = (f.geometry as GeoJSON.LineString).coordinates;
      const [a, b] = [c[1] as [number, number], c[2] as [number, number]];
      return Math.hypot(a[0] - b[0], a[1] - b[1]);
    };
    const lengths = fc.features.map(len);
    expect(Math.max(...lengths) / Math.min(...lengths)).toBeGreaterThan(1.5);
  });
});
