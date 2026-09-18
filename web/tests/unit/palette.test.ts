import { describe, expect, it } from "vitest";
import { BASEMAPS } from "@/config/basemaps";
import { type LayerGroupId, layerGroups } from "@/config/layers";
import type { ReportIndex } from "@/data/contract";
import { buildItems, PALETTE_GROUPS, type PaletteInput } from "@/features/palette/items";

const GROUPS = layerGroups();

function index(): ReportIndex {
  return {
    reports: [
      {
        file_num: "74H09-0039",
        company: "Colgan Resources",
        property: "Cluff Lake",
        era: "1970s",
        year: "x:74H09-0039:year",
        nts_sheets: ["74H09"],
        page_count: "x:74H09-0039:pages",
        scan_kind: "scanned",
        file_sha256: "a".repeat(64),
        source_url: "https://example.invalid/74H09-0039",
        split: "dev",
        status_counts: {
          pass: "d:74H09-0039:pass",
          flag: "d:74H09-0039:flag",
          miss: "d:74H09-0039:miss",
        },
        footprint: { type: "Polygon", coordinates: [[[-109, 58]]] },
        centroid: [-109, 58],
        holes: [
          {
            hole_id: "h1",
            name: "DF-44",
            status: "pass",
            lonlat: [-109.5, 58.2],
            position_source: "extracted_transformed",
            datum_basis: "printed",
          },
          {
            hole_id: "h2",
            name: "DF-45",
            status: "miss",
            lonlat: null,
            position_source: "none",
            datum_basis: "none",
          },
        ],
      },
      {
        file_num: "64L04-0075",
        company: "Numac Oil and Gas",
        era: "1980s-1990s",
        nts_sheets: ["64L04"],
        year: "x:64L04-0075:year",
        page_count: "x:64L04-0075:pages",
        scan_kind: "text",
        file_sha256: "b".repeat(64),
        source_url: "https://example.invalid/64L04-0075",
        split: "heldout",
        status_counts: {
          pass: "d:64L04-0075:pass",
          flag: "d:64L04-0075:flag",
          miss: "d:64L04-0075:miss",
        },
        footprint: { type: "Polygon", coordinates: [[[-105, 57]]] },
        centroid: [-105, 57],
        holes: [
          {
            hole_id: "h1",
            name: "WOL-03",
            status: "flag",
            lonlat: [-105.2, 57.4],
            position_source: "provincial_geods",
            datum_basis: "none",
          },
        ],
      },
    ],
    values: {},
  } as unknown as ReportIndex;
}

function input(patch: Partial<PaletteInput> = {}): PaletteInput {
  const visible = Object.fromEntries(GROUPS.map((g) => [g.id, g.defaultVisible])) as Record<
    LayerGroupId,
    boolean
  >;
  return {
    index: index(),
    visible,
    basemap: "ink",
    datum: { lens: false, misread: false },
    timelineOpen: false,
    ...patch,
  };
}

describe("buildItems", () => {
  it("emits every group, in display order", () => {
    const seen: string[] = [];
    for (const item of buildItems(input())) {
      if (seen[seen.length - 1] !== item.group) seen.push(item.group);
    }
    expect(seen).toEqual([...PALETTE_GROUPS]);
  });

  it("emits one row per report and one per hole of every report", () => {
    const items = buildItems(input());
    const reports = items.filter((i) => i.action.kind === "report");
    const holes = items.filter((i) => i.action.kind === "hole");
    expect(reports.map((i) => i.id)).toEqual(["report:74H09-0039", "report:64L04-0075"]);
    expect(holes.map((i) => i.id)).toEqual([
      "hole:74H09-0039:h1",
      "hole:74H09-0039:h2",
      "hole:64L04-0075:h1",
    ]);
    expect(holes.map((i) => i.title[0]?.text)).toEqual([
      "DF-44 · 74H09-0039",
      "DF-45 · 74H09-0039",
      "WOL-03 · 64L04-0075",
    ]);
    // an unplaced hole says so rather than pretending it can be flown to
    expect(holes.map((i) => i.placed)).toEqual([true, false, true]);
    expect(holes.map((i) => i.status)).toEqual(["pass", "miss", "flag"]);
  });

  it("leaves the report and hole groups empty when no index has loaded", () => {
    const items = buildItems(input({ index: null }));
    expect(items.filter((i) => i.group === "Reports" || i.group === "Holes")).toEqual([]);
    expect(items.some((i) => i.group === "Go")).toBe(true);
    expect(items.some((i) => i.group === "Tools")).toBe(true);
  });

  it("gives one row per layer group, reflecting the visibility map it was passed", () => {
    const allOff = Object.fromEntries(GROUPS.map((g) => [g.id, false])) as Record<LayerGroupId, boolean>;
    const off = buildItems(input({ visible: allOff })).filter((i) => i.group === "Layers");
    expect(off.map((i) => i.id)).toEqual(GROUPS.map((g) => `layer:${g.id}`));
    expect(off.every((i) => i.title[0]?.text === "Show")).toBe(true);
    expect(off.every((i) => i.on === false)).toBe(true);

    const allOn = Object.fromEntries(GROUPS.map((g) => [g.id, true])) as Record<LayerGroupId, boolean>;
    const on = buildItems(input({ visible: allOn })).filter((i) => i.group === "Layers");
    expect(on.every((i) => i.title[0]?.text === "Hide")).toBe(true);
    expect(on.every((i) => i.on === true)).toBe(true);
  });

  it("gives one row per basemap and marks the current one", () => {
    const rows = buildItems(input({ basemap: "none" })).filter((i) => i.group === "Basemap");
    expect(rows.map((i) => i.id)).toEqual(BASEMAPS.map((b) => `basemap:${b.id}`));
    expect(rows.filter((i) => i.on).map((i) => i.action)).toEqual([{ kind: "basemap", basemap: "none" }]);
  });

  it("offers the tools, reflecting what is already on", () => {
    const tools = buildItems(input()).filter((i) => i.group === "Tools");
    expect(tools.map((i) => i.id)).toEqual([
      "tool:lens",
      "tool:misread",
      "tool:timeline",
      "tool:tour",
      "tool:attribution",
      "tool:reset",
      "tool:clear",
    ]);
    expect(tools.find((i) => i.id === "tool:lens")?.title[0]?.text).toBe("Turn on the cursor lens");
    expect(tools.find((i) => i.id === "tool:lens")?.kbd).toBe("D");
    expect(tools.find((i) => i.id === "tool:misread")?.kbd).toBe("M");

    const lit = buildItems(input({ datum: { lens: true, misread: true }, timelineOpen: true }));
    const on = lit.filter((i) => i.group === "Tools");
    expect(on.find((i) => i.id === "tool:lens")?.title[0]?.text).toBe("Turn off the cursor lens");
    expect(on.find((i) => i.id === "tool:timeline")?.title[0]?.text).toBe("Close the timeline");
  });

  it("gives every row a unique id and a unique, non-empty search value", () => {
    const items = buildItems(input());
    expect(new Set(items.map((i) => i.id)).size).toBe(items.length);
    expect(new Set(items.map((i) => i.value)).size).toBe(items.length);
    expect(items.every((i) => i.value.trim().length > 0)).toBe(true);
    expect(items.every((i) => i.value === i.value.replace(/\s{2,}/g, " ").trim())).toBe(true);
  });

  it("puts the searchable words in the value string", () => {
    const items = buildItems(input());
    const report = items.find((i) => i.id === "report:74H09-0039");
    expect(report?.value).toContain("74H09-0039");
    expect(report?.value).toContain("Colgan Resources");
    expect(report?.value).toContain("1970s");
    expect(report?.value).toContain("Cluff Lake");

    const hole = items.find((i) => i.id === "hole:64L04-0075:h1");
    expect(hole?.value).toContain("WOL-03");
    expect(hole?.value).toContain("64L04-0075");
    expect(hole?.value).toContain("Numac Oil and Gas");

    const layer = items.find((i) => i.id === "layer:compilation");
    const compilation = GROUPS.find((g) => g.id === "compilation");
    expect(layer?.value).toContain(compilation?.label);

    expect(items.find((i) => i.id === "go:/eval")?.value).toContain("/eval");
  });

  it("marks every digit-bearing span as an identifier", () => {
    for (const item of buildItems(input())) {
      for (const part of [...item.title, ...item.detail]) {
        if (/\d/.test(part.text)) expect(part.ident, `${item.id}: ${part.text}`).toBe(true);
      }
    }
  });
});
