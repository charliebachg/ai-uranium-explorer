import { describe, expect, it } from "vitest";
import { layerGroups, withTiles } from "@/config/layers";
import type { TilesManifest } from "@/data/tiles";

const entry = (id: string) => ({
  file: `${id}.pmtiles`,
  bytes: 10,
  sha256: "a".repeat(64),
  minzoom: 4,
  maxzoom: 12,
  layer: id,
  features: 3,
  source_geojson: `context/${id}.geojson`,
  source_sha256: "b".repeat(64),
});
const manifest: TilesManifest = {
  version: "tiles/v1",
  built_at: "t",
  tippecanoe: "v",
  rules: "r",
  skipped: [],
  tiles: { conductors: entry("conductors"), cells: entry("cells") },
};

describe("withTiles", () => {
  it("swaps a covered source for its archive and gives its layers a source-layer", () => {
    const groups = withTiles(layerGroups(), manifest, "http://test.local");
    const conductors = groups.find((g) => g.id === "conductors");
    expect(conductors?.sources.conductors).toEqual({
      type: "vector",
      url: "pmtiles://http://test.local/data/tiles/conductors.pmtiles",
      minzoom: 4,
      maxzoom: 12,
    });
    for (const l of conductors?.layers ?? [])
      expect((l as { "source-layer"?: string })["source-layer"]).toBe("conductors");
    expect(conductors?.lazy).toBeUndefined();
  });

  it("leaves groups the manifest does not cover exactly as they were", () => {
    const before = layerGroups();
    const after = withTiles(before, manifest, "http://test.local");
    const faults = (gs: typeof before) => gs.find((g) => g.id === "faults");
    expect(faults(after)).toBe(faults(before));
    expect(faults(after)?.lazy).toBeDefined();
  });

  it("is the identity without a manifest", () => {
    const before = layerGroups();
    expect(withTiles(before, null)).toBe(before);
  });

  it("keeps the score cells' declared colour exception on the tiled source", () => {
    const groups = withTiles(layerGroups(), manifest, "http://test.local");
    const prospect = groups.find((g) => g.id === "prospect");
    expect(prospect?.sources.cells).toMatchObject({ type: "vector" });
    const scored = prospect?.layers.find(
      (l) => (l as { metadata?: Record<string, unknown> }).metadata?.["ue:score"],
    );
    expect(scored).toBeDefined();
    expect((scored as { "source-layer"?: string })["source-layer"]).toBe("cells");
  });
});
