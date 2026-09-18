import { BASEMAPS, type BasemapId } from "@/config/basemaps";
import { type LayerGroupId, layerGroups } from "@/config/layers";
import { WORDING } from "@/config/wording";
import type { LonLat, ReportIndex } from "@/data/contract";

/**
 * The command palette's item list, built as plain data so it can be unit-tested without a DOM or a map.
 * Nothing here prints a stored number: report and hole rows carry identifiers only (file numbers, hole names),
 * and every span that holds digits is marked `ident` so the strict-numbers walk lets it through.
 */

export const PALETTE_GROUPS = ["Go", "Reports", "Holes", "Layers", "Basemap", "Tools"] as const;
export type PaletteGroup = (typeof PALETTE_GROUPS)[number];

export type ToolId = "lens" | "misread" | "timeline" | "tour" | "attribution" | "reset" | "clear";

export type PaletteAction =
  | { kind: "go"; href: string }
  | { kind: "report"; file: string }
  | { kind: "hole"; file: string; hole: string; lonlat: LonLat | null }
  | { kind: "layer"; layer: LayerGroupId }
  | { kind: "basemap"; basemap: BasemapId }
  | { kind: "tool"; tool: ToolId };

/** One span of a row. `ident` marks text the strict-numbers rule reads as an identifier, not a measurement. */
export type Part = { text: string; ident?: boolean };

export interface PaletteItem {
  id: string;
  group: PaletteGroup;
  /** Primary line, rendered space-separated. */
  title: Part[];
  /** Secondary line, rendered with a middle dot between parts. */
  detail: Part[];
  /** What cmdk searches and identifies the row by: unique, and carrying every searchable word. */
  value: string;
  /** Keyboard hint for the row (chrome, never a stored number). */
  kbd?: string;
  /** Holes: extraction status, shown as a StatusMark. */
  status?: "pass" | "flag" | "miss";
  /** Holes: false when the report prints no position the pipeline could place. */
  placed?: boolean;
  /** True when the thing the row toggles is already on (a visible layer, the current basemap). */
  on?: boolean;
  action: PaletteAction;
}

export interface PaletteInput {
  index: ReportIndex | null;
  visible: Record<LayerGroupId, boolean>;
  basemap: BasemapId;
  datum: { lens: boolean; misread: boolean };
  timelineOpen: boolean;
}

const GO: { href: string; label: string; detail: string }[] = [
  { href: "/", label: "Map", detail: "Reports, holes and the provincial compilations" },
  { href: "/eval", label: "Eval", detail: "How often the reader reads a printed value wrongly" },
  { href: "/limits", label: "Limits", detail: "What this demo does not claim to do" },
];

const SCAN_TEXT: Record<string, string> = { scanned: "scanned", text: "born-digital", mixed: "mixed" };

function words(...parts: (string | null | undefined)[]): string {
  return parts
    .filter((p): p is string => !!p)
    .join(" ")
    .replace(/\s+/g, " ")
    .trim();
}

/** Every palette row, grouped in display order. Pure: the caller passes the state it should reflect. */
export function buildItems(input: PaletteInput): PaletteItem[] {
  const items: PaletteItem[] = [];

  for (const g of GO) {
    items.push({
      id: `go:${g.href}`,
      group: "Go",
      title: [{ text: g.label }],
      detail: [{ text: g.detail }],
      value: words("go", g.label, g.href, g.detail),
      action: { kind: "go", href: g.href },
    });
  }

  const reports = input.index?.reports ?? [];

  for (const r of reports) {
    items.push({
      id: `report:${r.file_num}`,
      group: "Reports",
      title: [{ text: r.file_num, ident: true }],
      detail: [{ text: r.company }, { text: r.era, ident: true }, { text: SCAN_TEXT[r.scan_kind] ?? "" }],
      value: words(
        "report file",
        r.file_num,
        r.company,
        r.property,
        r.era,
        SCAN_TEXT[r.scan_kind],
        r.nts_sheets.join(" "),
      ),
      action: { kind: "report", file: r.file_num },
    });
  }

  for (const r of reports) {
    for (const h of r.holes) {
      items.push({
        id: `hole:${r.file_num}:${h.hole_id}`,
        group: "Holes",
        title: [{ text: `${h.name} · ${r.file_num}`, ident: true }],
        detail: [{ text: r.company }],
        value: words("hole", h.name, r.file_num, r.company, h.hole_id),
        status: h.status,
        placed: !!h.lonlat,
        action: { kind: "hole", file: r.file_num, hole: h.hole_id, lonlat: h.lonlat },
      });
    }
  }

  for (const g of layerGroups()) {
    const on = !!input.visible[g.id];
    items.push({
      id: `layer:${g.id}`,
      group: "Layers",
      title: [{ text: on ? "Hide" : "Show" }, { text: g.label, ident: true }],
      detail: [{ text: g.detail, ident: true }],
      value: words("layer show hide", g.label, g.detail, g.id),
      on,
      action: { kind: "layer", layer: g.id },
    });
  }

  for (const b of BASEMAPS) {
    items.push({
      id: `basemap:${b.id}`,
      group: "Basemap",
      title: [{ text: b.label }],
      detail: [{ text: b.description }],
      value: words("basemap", b.label, b.id, b.description),
      on: input.basemap === b.id,
      action: { kind: "basemap", basemap: b.id },
    });
  }

  const tools: { tool: ToolId; title: string; detail: Part[]; kbd?: string; on?: boolean }[] = [
    {
      tool: "lens",
      title: input.datum.lens ? "Turn off the cursor lens" : "Turn on the cursor lens",
      detail: [{ text: "Shift vector under the cursor" }],
      kbd: "D",
      on: input.datum.lens,
    },
    {
      tool: "misread",
      title: input.datum.misread ? "Hide the misread datum" : "Show the misread datum",
      detail: [{ text: "Where NAD27 collars land if read as NAD83", ident: true }],
      kbd: "M",
      on: input.datum.misread,
    },
    {
      tool: "timeline",
      title: input.timelineOpen ? "Close the timeline" : "Open the timeline",
      detail: [{ text: WORDING.timelineCaption }],
      on: input.timelineOpen,
    },
    {
      tool: "tour",
      title: "Start the guided tour",
      detail: [{ text: "A short walk through what this demo reads" }],
    },
    {
      tool: "attribution",
      title: "Data sources and licences",
      detail: [{ text: "Who publishes each layer, and what it may be used for" }],
    },
    {
      tool: "reset",
      title: "Reset the view",
      detail: [{ text: "Fly back to the opening camera" }],
    },
    {
      tool: "clear",
      title: "Clear the selection",
      detail: [{ text: "Drop the selected feature" }],
    },
  ];

  for (const t of tools) {
    items.push({
      id: `tool:${t.tool}`,
      group: "Tools",
      title: [{ text: t.title }],
      detail: t.detail,
      value: words("tool", t.title, t.detail.map((p) => p.text).join(" "), t.tool),
      kbd: t.kbd,
      on: t.on,
      action: { kind: "tool", tool: t.tool },
    });
  }

  return items;
}
