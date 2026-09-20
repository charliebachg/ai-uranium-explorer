import type { StyleSpecification } from "maplibre-gl";
import { describe, expect, it } from "vitest";
import { BASEMAPS } from "@/config/basemaps";
import { type LayerGroupId, layerGroups } from "@/config/layers";
import { composeStyle, honestyViolations } from "@/map/style/composeStyle";
import { inkStyle } from "@/map/style/inkStyle";

const groups = layerGroups();
const visible = Object.fromEntries(groups.map((g) => [g.id, true])) as Record<LayerGroupId, boolean>;
const ink = BASEMAPS.find((b) => b.id === "ink")!;
const none = BASEMAPS.find((b) => b.id === "none")!;

describe("composeStyle", () => {
  it("injects app layers before the basemap labels on ink", () => {
    const style = composeStyle({ base: inkStyle(), basemap: ink, groups, visible });
    const ids = style.layers.map((l) => l.id);
    expect(ids.indexOf("relief-hillshade")).toBeLessThan(ids.indexOf("water"));
    expect(ids.indexOf("compilation-dot")).toBeGreaterThan(ids.indexOf("water"));
    expect(ids.indexOf("compilation-dot")).toBeLessThan(ids.indexOf("place-settlement"));
    expect(ids.indexOf("basin-line")).toBeLessThan(ids.indexOf("compilation-dot"));
  });

  it("appends app layers in slot order when the basemap has no anchors", async () => {
    const style = composeStyle({ base: await none.load(), basemap: none, groups, visible });
    const ids = style.layers.map((l) => l.id);
    expect(ids[0]).toBe("background");
    expect(ids.indexOf("relief-color")).toBeLessThan(ids.indexOf("basin-fill"));
    expect(ids.indexOf("basin-fill")).toBeLessThan(ids.indexOf("geods-dot"));
  });

  it("bakes visibility into layout and sets global-state defaults", () => {
    const hidden = { ...visible, nts: false };
    const style = composeStyle({
      base: inkStyle(),
      basemap: ink,
      groups,
      visible: hidden,
      globalState: { uraniumOnly: true },
    });
    const nts = style.layers.find((l) => l.id === "nts-250k") as { layout?: { visibility?: string } };
    expect(nts.layout?.visibility).toBe("none");
    expect(
      (style as StyleSpecification & { state: Record<string, { default: unknown }> }).state.uraniumOnly
        ?.default,
    ).toBe(true);
  });

  it("passes the honesty checks: no heatmaps, colour only from status or source", () => {
    const style = composeStyle({ base: inkStyle(), basemap: ink, groups, visible });
    expect(honestyViolations(style)).toEqual([]);
  });

  it("flags a heatmap and a grade-driven colour", () => {
    const bad = {
      version: 8,
      sources: {},
      layers: [
        { id: "h", type: "heatmap", source: "x" },
        {
          id: "c",
          type: "circle",
          source: "x",
          paint: { "circle-color": ["interpolate", ["linear"], ["get", "grade"], 0, "#000", 1, "#f00"] },
        },
      ],
    } as unknown as StyleSpecification;
    const v = honestyViolations(bad);
    expect(v.some((m) => m.includes("heatmap"))).toBe(true);
    expect(v.some((m) => m.includes('"grade"'))).toBe(true);
  });

  it("never references the mineral dispositions service", () => {
    const json = JSON.stringify(groups);
    expect(json).not.toContain("Mining/MapServer");
  });
});

describe("the score colour exception", () => {
  const style = () => composeStyle({ base: inkStyle(), basemap: ink, groups, visible });

  it("lets the declared score layer colour by a score, and the real style stays clean", () => {
    expect(honestyViolations(style())).toEqual([]);
    const cell = style().layers.find((l) => l.id === "prospect-cell");
    expect((cell?.metadata as Record<string, unknown>)["ue:score"]).toBe(true);
  });

  it("refuses a layer that colours by a computed key without declaring itself", () => {
    const sneaky = style();
    const layer = sneaky.layers.find((l) => l.id === "prospect-cell") as Record<string, unknown>;
    layer.metadata = { "ue:group": "prospect" };
    expect(honestyViolations(sneaky).join(" ")).toContain('without declaring metadata["ue:score"]');
  });

  it("refuses a declared score layer that colours by something that is not a score", () => {
    const sneaky = style();
    const layer = sneaky.layers.find((l) => l.id === "prospect-cell") as {
      paint: Record<string, unknown>;
    };
    layer.paint["circle-color"] = ["match", ["get", "grade"], "high", "#fff", "#000"];
    expect(honestyViolations(sneaky).join(" ")).toContain('colours by "grade", which is not a score');
  });

  it("still refuses an ordinary layer coloured by a data field", () => {
    const sneaky = style();
    const layer = sneaky.layers.find((l) => l.id === "compilation-dot") as {
      paint: Record<string, unknown>;
    };
    layer.paint["circle-color"] = ["match", ["get", "grade"], "high", "#fff", "#000"];
    expect(honestyViolations(sneaky).join(" ")).toContain('colour driven by data field "grade"');
  });
});
