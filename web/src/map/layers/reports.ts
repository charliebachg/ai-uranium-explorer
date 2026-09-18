import type { FeatureCollection, Polygon } from "geojson";
import type { LayerSpecification, SourceSpecification } from "maplibre-gl";
import type { ReportIndex } from "@/data/contract";

/**
 * Map data for the reports read by the pipeline: footprints, a spotlight mask for the open report, and read holes.
 * Geometry only; every number shown about these features comes from the report's value registry.
 */

export const EMPTY_FC: FeatureCollection = { type: "FeatureCollection", features: [] };

export type ReportMapData = {
  footprints: FeatureCollection;
  holes: FeatureCollection;
  mask: FeatureCollection;
};

export function reportMapData(index: ReportIndex | null, openReport: string | null): ReportMapData {
  if (!index) return { footprints: EMPTY_FC, holes: EMPTY_FC, mask: EMPTY_FC };
  const footprints: FeatureCollection = {
    type: "FeatureCollection",
    features: index.reports.map((r, i) => ({
      type: "Feature",
      id: i + 1,
      properties: {
        file: r.file_num,
        era: r.era,
        split: r.split,
        placed: r.holes.some((h) => h.lonlat) ? 1 : 0,
      },
      geometry: r.footprint as Polygon,
    })),
  };
  const holes: FeatureCollection = { type: "FeatureCollection", features: [] };
  let hid = 0;
  for (const r of index.reports) {
    for (const h of r.holes) {
      if (!h.lonlat) continue;
      hid += 1;
      holes.features.push({
        type: "Feature",
        id: hid,
        properties: {
          file: r.file_num,
          hole: h.hole_id,
          name: h.name,
          status: h.status,
          src: h.position_source,
        },
        geometry: { type: "Point", coordinates: h.lonlat },
      });
    }
  }
  const open = openReport ? index.reports.find((r) => r.file_num === openReport) : null;
  const mask: FeatureCollection = open
    ? {
        type: "FeatureCollection",
        features: [
          {
            type: "Feature",
            properties: {},
            geometry: {
              type: "Polygon",
              coordinates: [
                [
                  [-179.9, -85],
                  [179.9, -85],
                  [179.9, 85],
                  [-179.9, 85],
                  [-179.9, -85],
                ],
                // the footprint ring, reversed, cuts the spotlight hole
                [...((open.footprint.coordinates[0] ?? []) as [number, number][])].reverse(),
              ],
            },
          },
        ],
      }
    : EMPTY_FC;
  return { footprints, holes, mask };
}

const STATUS_COLOR = [
  "match",
  ["get", "status"],
  "pass",
  "#e6f0ff",
  "flag",
  "#fab219",
  "miss",
  "#ff5fa2",
  "#8b97a6",
];

export function reportSources(d: ReportMapData): Record<string, SourceSpecification> {
  return {
    "rep-footprints": { type: "geojson", data: d.footprints },
    "rep-mask": { type: "geojson", data: d.mask },
    "rep-holes": { type: "geojson", data: d.holes },
  };
}

export const REPORT_CONTEXT_LAYERS: LayerSpecification[] = [
  {
    id: "fp-mask",
    type: "fill",
    source: "rep-mask",
    paint: { "fill-color": "#000000", "fill-opacity": 0.45, "fill-opacity-transition": { duration: 320 } },
  },
  {
    id: "fp-fill",
    type: "fill",
    source: "rep-footprints",
    paint: {
      "fill-color": "#e6f0ff",
      "fill-opacity": [
        "case",
        ["boolean", ["feature-state", "selected"], false],
        0.07,
        ["boolean", ["feature-state", "hover"], false],
        0.12,
        0.04,
      ],
    },
  },
  {
    id: "fp-line",
    type: "line",
    source: "rep-footprints",
    paint: {
      "line-color": "#e6f0ff",
      "line-opacity": ["case", ["boolean", ["feature-state", "selected"], false], 0.9, 0.55],
      "line-width": ["case", ["boolean", ["feature-state", "selected"], false], 2.2, 1.2],
      "line-dasharray": [3, 2],
    },
  },
];

export const REPORT_DATA_LAYERS: LayerSpecification[] = [
  {
    id: "rh-halo",
    type: "circle",
    source: "rep-holes",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 7, 12, 14],
      "circle-color": STATUS_COLOR as never,
      "circle-blur": 1,
      "circle-opacity": 0.35,
    },
  },
  {
    id: "rh-core",
    type: "circle",
    source: "rep-holes",
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 3.5, 12, 6.5, 16, 9],
      // shape carries status as well as colour: pass filled, flag ring with a centre dot, miss hollow ring with a cross
      "circle-color": ["match", ["get", "status"], "pass", "#e6f0ff", "rgba(0,0,0,0)"],
      "circle-stroke-color": STATUS_COLOR as never,
      "circle-stroke-width": [
        "case",
        ["boolean", ["feature-state", "selected"], false],
        3,
        ["boolean", ["feature-state", "hover"], false],
        2.6,
        ["match", ["get", "status"], "pass", 0, 1.8],
      ],
    },
  },
  {
    id: "rh-dot",
    type: "circle",
    source: "rep-holes",
    filter: ["==", ["get", "status"], "flag"],
    paint: {
      "circle-radius": ["interpolate", ["linear"], ["zoom"], 6, 1.2, 12, 2.2],
      "circle-color": "#fab219",
    },
  },
  {
    id: "rh-cross",
    type: "symbol",
    source: "rep-holes",
    filter: ["==", ["get", "status"], "miss"],
    layout: {
      "text-field": "×",
      "text-font": ["Noto Sans Regular"],
      "text-size": ["interpolate", ["linear"], ["zoom"], 6, 9, 12, 14],
      "text-allow-overlap": true,
      "text-ignore-placement": true,
    },
    paint: { "text-color": "#ff5fa2" },
  },
  {
    id: "rh-label",
    type: "symbol",
    source: "rep-holes",
    minzoom: 11.5,
    layout: {
      "text-field": ["get", "name"],
      "text-font": ["Noto Sans Regular"],
      "text-size": 11,
      "text-offset": [0, 1.4],
      "text-anchor": "top",
    },
    paint: { "text-color": "#c9d6e6", "text-halo-color": "#0b0f14", "text-halo-width": 1.4 },
  },
];
