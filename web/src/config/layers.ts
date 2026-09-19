import type { LayerSpecification, SourceSpecification } from "maplibre-gl";
import { dataUrl } from "@/data/loader";
import { type TilesManifest, tileSource } from "@/data/tiles";
import {
  EMPTY_FC,
  REPORT_CONTEXT_LAYERS,
  REPORT_DATA_LAYERS,
  type ReportMapData,
  reportSources,
} from "@/map/layers/reports";
import { RELIEF } from "./basemaps";

/**
 * App layer registry. Colour encodes data source (and, from Phase 2, extraction status) only: never grade,
 * commodity or potential. tests/unit/composeStyle.test.ts enforces that on the composed style.
 */

export const COLORS = {
  compilation: "#3987e5",
  geods: "#199e70",
  pass: "#e6f0ff",
  flag: "#fab219",
  miss: "#ff5fa2",
  ice: "#c9d6e6",
  neutral: "#8b97a6",
  ground: "#0b0f14",
} as const;

/**
 * The score ramp, dark to pale, and the diverging ramp for "learned minus effort". Kept beside the layer that
 * uses them because they are the one place in this app where colour carries a number.
 */
export type Theme = "dark" | "light";

/**
 * The map colours that have to change with the theme. Everything else in COLORS is a source or status hue
 * chosen to read on either ground, and stays put — a data source that changed colour with the theme would make
 * the legend a lie.
 *
 * The score ramp flips rather than lightens: on a dark ground high scores are pale, on a light ground they are
 * dark. What is preserved is that **high score = more ink**, and that the ramp's quiet end still sits clearly
 * above the background, because a cell that scored zero must never look like a cell with no score at all.
 */
const THEMED = {
  dark: {
    ice: "#c9d6e6",
    pass: "#e6f0ff",
    gap: "#8b97a6",
    ramp: ["#2b3440", "#44505f", "#6b7683", "#a4afbc", "#e6edf5"],
    below: "#ff5fa2",
    middle: "#1e2632",
    above: "#6ea4ff",
    relief: ["#0b0f14", "#0f141b", "#151c25"],
    reliefOpacity: 0.85,
    shadow: "#03050a",
    highlight: "#2b3848",
  },
  light: {
    ice: "#43566f",
    pass: "#2f4058",
    gap: "#7d8894",
    ramp: ["#c9d2de", "#a3afbf", "#748294", "#455467", "#111925"],
    below: "#c2185b",
    middle: "#e7ecf2",
    above: "#1d4ed8",
    // the dark ramp painted sea level near-black, which swamped a pale basemap and turned every lake navy
    relief: ["#eef1f6", "#e4e9f0", "#d8e0ea"],
    reliefOpacity: 0.5,
    shadow: "#8792a2",
    highlight: "#ffffff",
  },
} as const satisfies Record<Theme, Record<string, unknown>>;

export const scoreRamp = (theme: Theme): readonly string[] => THEMED[theme].ramp;
export const divergingSwatches = (theme: Theme): readonly string[] => {
  const t = THEMED[theme];
  return [t.below, t.middle, t.above];
};

const SCORE = ["to-number", ["get", ["global-state", "scoreKey"]]] as unknown as number;

const sequential = (theme: Theme) => {
  const r = THEMED[theme].ramp;
  return [
    "interpolate",
    ["linear"],
    SCORE,
    0,
    r[0],
    0.25,
    r[1],
    0.5,
    r[2],
    0.75,
    r[3],
    1,
    r[4],
  ] as unknown as string;
};

/** Pink where exploration effort leads the learned score, blue where the learned score leads it. */
const diverging = (theme: Theme) => {
  const t = THEMED[theme];
  return ["interpolate", ["linear"], SCORE, -0.6, t.below, 0, t.middle, 0.6, t.above] as unknown as string;
};

/** One 2 km cell, in pixels: big enough to read as a grid, small enough not to smear at basin zoom. */
const CELL_RADIUS = [
  "interpolate",
  ["exponential", 2],
  ["zoom"],
  4,
  1.2,
  7,
  3.4,
  10,
  12,
  13,
  90,
] as unknown as number;

export type Slot = "relief" | "context" | "data" | "top";
export type LayerGroupId =
  | "relief"
  | "prospect"
  | "conductors"
  | "faults"
  | "host"
  | "lakeSediment"
  | "lakeWater"
  | "boulders"
  | "surveysAir"
  | "surveysGround"
  | "basin"
  | "nts"
  | "deposits"
  | "occurrences"
  | "geods"
  | "compilation"
  | "reports"
  | "readHoles"
  | "datumField";

export interface LayerGroup {
  id: LayerGroupId;
  label: string;
  detail: string;
  sourceId: string | null; // manifest source id, for counts and attribution
  countStat: string | null; // manifest stat id shown next to the toggle
  legend: { kind: "dot" | "line" | "fill" | "relief" | "grid" | "hollow"; color: string };
  defaultVisible: boolean;
  slot: Slot;
  sources: Record<string, SourceSpecification>;
  layers: LayerSpecification[];
  hit: string[]; // layer ids that answer hover and click
  /**
   * {sourceId: url} for evidence layers that are fetched the first time the group is switched on. Together
   * these are 16 MB; a reader who never opens them should not pay for them, and MapLibre fetches a geojson
   * source as soon as it is in the style whatever the layer's visibility says. So the source starts empty and
   * the controller calls setData on first use.
   */
  lazy?: Record<string, string>;
}

const hoverStroke = (base: number, hover: number) =>
  [
    "case",
    ["boolean", ["feature-state", "selected"], false],
    hover + 1,
    ["boolean", ["feature-state", "hover"], false],
    hover,
    base,
  ] as unknown as number;

export function layerGroups(
  reportData: ReportMapData = { footprints: EMPTY_FC, holes: EMPTY_FC, mask: EMPTY_FC },
  theme: Theme = "dark",
): LayerGroup[] {
  const repSources = reportSources(reportData);
  const t = THEMED[theme];
  return [
    {
      id: "prospect",
      label: "Prospect scores",
      detail: "Retrospective scores over 2 km cells. Colour is a score here, not a data source.",
      sourceId: null,
      // the cell count lives in the readiness export, not the manifest this rail reads
      countStat: null,
      legend: { kind: "grid", color: t.ramp[4] },
      defaultVisible: false,
      slot: "context",
      sources: { cells: { type: "geojson", data: dataUrl("prospect/scores.geojson") } },
      layers: [
        {
          // a cell the chosen model could not score is an outline with no fill: a hole in the picture, never
          // a low score. Drawn first so a scored neighbour sits on top of it.
          id: "prospect-gap",
          type: "circle",
          source: "cells",
          filter: ["!", ["has", ["global-state", "scoreKey"]]],
          paint: {
            "circle-radius": CELL_RADIUS,
            "circle-color": "transparent",
            "circle-stroke-color": t.gap,
            "circle-stroke-opacity": 0.3,
            "circle-stroke-width": 0.6,
          },
        },
        {
          id: "prospect-cell",
          type: "circle",
          source: "cells",
          filter: ["has", ["global-state", "scoreKey"]],
          // THE DOCUMENTED COLOUR EXCEPTION. Everywhere else in this app colour means a data source or an
          // extraction status. Here it means a score, which is exactly the thing that invites being read as
          // prospectivity — so the layer declares itself, the honesty check allows only these fields on a
          // layer that does, and the banner above the map says what the colour is.
          metadata: { "lr:score": true },
          paint: {
            "circle-radius": CELL_RADIUS,
            "circle-color": [
              "case",
              ["==", ["global-state", "scoreKey"], "d"],
              diverging(theme),
              sequential(theme),
            ],
            "circle-opacity": ["interpolate", ["linear"], ["zoom"], 4, 0.85, 8, 0.95],
            "circle-stroke-color": "#ffffff",
            "circle-stroke-width": [
              "case",
              ["boolean", ["feature-state", "selected"], false],
              1.6,
              ["boolean", ["feature-state", "hover"], false],
              0.9,
              0,
            ],
          },
        },
      ],
      hit: ["prospect-cell", "prospect-gap"],
    },
    {
      id: "conductors",
      label: "EM conductors",
      detail: "Mapped electromagnetic conductors: the pathway criterion's own input",
      sourceId: "em_conductors",
      countStat: null,
      legend: { kind: "line", color: t.ice },
      defaultVisible: false,
      slot: "context",
      sources: { conductors: { type: "geojson", data: EMPTY_FC } },
      lazy: { conductors: dataUrl("context/em_conductors.geojson") },
      layers: [
        {
          id: "conductor-line",
          type: "line",
          source: "conductors",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": t.ice,
            "line-opacity": ["interpolate", ["linear"], ["zoom"], 5, 0.22, 8, 0.4, 12, 0.6],
            "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.35, 10, 1.2, 13, 2],
          },
        },
      ],
      hit: [],
    },
    {
      id: "faults",
      label: "Faults and lineaments",
      detail: "Mapped structure from the provincial bedrock sheet; the trap criterion's input",
      sourceId: "faults_250k",
      countStat: null,
      legend: { kind: "line", color: COLORS.neutral },
      defaultVisible: false,
      slot: "context",
      sources: { faults: { type: "geojson", data: EMPTY_FC } },
      lazy: { faults: dataUrl("context/faults.geojson") },
      layers: [
        {
          id: "fault-line",
          type: "line",
          source: "faults",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": COLORS.neutral,
            "line-opacity": ["interpolate", ["linear"], ["zoom"], 5, 0.3, 9, 0.5],
            "line-dasharray": [3, 2],
            "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.4, 10, 1.2],
          },
        },
      ],
      hit: [],
    },
    {
      id: "host",
      label: "Graphitic or pelitic host",
      detail: "Bedrock units whose mapped lithology names the host rock",
      sourceId: "bedrock_250k",
      countStat: null,
      legend: { kind: "fill", color: COLORS.neutral },
      defaultVisible: false,
      slot: "context",
      sources: { host: { type: "geojson", data: EMPTY_FC } },
      lazy: { host: dataUrl("context/graphitic_host.geojson") },
      layers: [
        {
          id: "host-fill",
          type: "fill",
          source: "host",
          paint: { "fill-color": COLORS.neutral, "fill-opacity": 0.22 },
        },
        {
          id: "host-line",
          type: "line",
          source: "host",
          paint: { "line-color": COLORS.neutral, "line-opacity": 0.3, "line-width": 0.6 },
        },
      ],
      hit: [],
    },
    {
      id: "lakeSediment",
      label: "Lake sediment samples",
      detail: "Where the lake-sediment survey sampled. Size is uranium; colour stays the source's.",
      sourceId: "lake_sediment_sgs",
      countStat: null,
      legend: { kind: "dot", color: COLORS.geods },
      defaultVisible: false,
      slot: "data",
      sources: { lakesed: { type: "geojson", data: EMPTY_FC } },
      lazy: { lakesed: dataUrl("context/lake_sediment_u.geojson") },
      layers: [
        {
          id: "lakesed-dot",
          type: "circle",
          source: "lakesed",
          paint: {
            "circle-color": COLORS.geods,
            "circle-opacity": 0.75,
            // size carries the reading; colour still says which survey it came from
            "circle-radius": [
              "interpolate",
              ["linear"],
              ["zoom"],
              5,
              ["interpolate", ["linear"], ["coalesce", ["get", "u"], 0], 0, 1.2, 50, 4],
              11,
              ["interpolate", ["linear"], ["coalesce", ["get", "u"], 0], 0, 3, 50, 12],
            ],
          },
        },
      ],
      hit: [],
    },
    {
      id: "lakeWater",
      label: "Lake water samples",
      detail: "Thin coverage: this survey reaches a small fraction of the grid",
      sourceId: "lake_water_sgs",
      countStat: null,
      legend: { kind: "dot", color: t.pass },
      defaultVisible: false,
      slot: "data",
      sources: { lakewater: { type: "geojson", data: EMPTY_FC } },
      lazy: { lakewater: dataUrl("context/lake_water_u.geojson") },
      layers: [
        {
          id: "lakewater-dot",
          type: "circle",
          source: "lakewater",
          paint: {
            "circle-color": t.pass,
            "circle-opacity": 0.8,
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 1.6, 11, 5],
          },
        },
      ],
      hit: [],
    },
    {
      id: "boulders",
      label: "Radioactive boulders",
      detail: "Boulders found at surface. The source lies up-ice, not underneath.",
      sourceId: "radioactive_boulders",
      countStat: null,
      legend: { kind: "dot", color: COLORS.flag },
      defaultVisible: false,
      slot: "data",
      sources: { boulders: { type: "geojson", data: EMPTY_FC } },
      lazy: { boulders: dataUrl("context/radioactive_boulders.geojson") },
      layers: [
        {
          id: "boulder-dot",
          type: "circle",
          source: "boulders",
          paint: {
            "circle-color": COLORS.flag,
            "circle-opacity": 0.7,
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 5, 1.4, 11, 4.5],
          },
        },
      ],
      hit: [],
    },
    {
      id: "surveysAir",
      label: "Airborne survey footprints",
      detail: "Where somebody flew a survey. Exploration effort, not rock.",
      sourceId: "assessment_surveys",
      countStat: null,
      legend: { kind: "hollow", color: COLORS.compilation },
      defaultVisible: false,
      slot: "context",
      sources: { surveyair: { type: "geojson", data: EMPTY_FC } },
      lazy: { surveyair: dataUrl("context/surveys_airborne.geojson") },
      layers: [
        {
          id: "surveyair-line",
          type: "line",
          source: "surveyair",
          paint: { "line-color": COLORS.compilation, "line-opacity": 0.28, "line-width": 0.7 },
        },
      ],
      hit: [],
    },
    {
      id: "surveysGround",
      label: "Ground survey footprints",
      detail: "Where somebody walked a survey. Exploration effort, not rock.",
      sourceId: "assessment_surveys",
      countStat: null,
      legend: { kind: "hollow", color: COLORS.geods },
      defaultVisible: false,
      slot: "context",
      sources: { surveyground: { type: "geojson", data: EMPTY_FC } },
      lazy: { surveyground: dataUrl("context/surveys_ground.geojson") },
      layers: [
        {
          id: "surveyground-line",
          type: "line",
          source: "surveyground",
          paint: { "line-color": COLORS.geods, "line-opacity": 0.28, "line-width": 0.7 },
        },
      ],
      hit: [],
    },
    {
      id: "relief",
      label: "Relief shading",
      detail: "Hillshade from Mapzen Terrain Tiles; monochrome, not data",
      sourceId: null,
      countStat: null,
      legend: { kind: "relief", color: COLORS.neutral },
      defaultVisible: true,
      slot: "relief",
      sources: {
        dem: {
          type: "raster-dem",
          tiles: [RELIEF.tiles],
          encoding: RELIEF.encoding,
          tileSize: RELIEF.tileSize,
          maxzoom: RELIEF.maxzoom,
          attribution: RELIEF.attribution,
        },
      },
      layers: [
        {
          id: "relief-color",
          type: "color-relief",
          source: "dem",
          paint: {
            "color-relief-color": [
              "interpolate",
              ["linear"],
              ["elevation"],
              150,
              t.relief[0],
              450,
              t.relief[1],
              900,
              t.relief[2],
            ],
            "color-relief-opacity": t.reliefOpacity,
          },
        },
        {
          id: "relief-hillshade",
          type: "hillshade",
          source: "dem",
          paint: {
            "hillshade-method": "multidirectional",
            "hillshade-shadow-color": t.shadow,
            "hillshade-highlight-color": t.highlight,
            "hillshade-exaggeration": ["interpolate", ["linear"], ["zoom"], 4, 0.25, 9, 0.5, 13, 0.35],
          },
        },
      ],
      hit: [],
    },
    {
      id: "basin",
      label: "Athabasca Basin",
      detail: "Athabasca Supergroup, 1:1,000,000 bedrock geology",
      sourceId: "basin_geology",
      countStat: null,
      legend: { kind: "line", color: t.ice },
      defaultVisible: true,
      slot: "context",
      sources: { basin: { type: "geojson", data: dataUrl("context/basin.geojson") } },
      layers: [
        {
          id: "basin-fill",
          type: "fill",
          source: "basin",
          filter: ["==", ["get", "role"], "area"],
          paint: { "fill-color": "#ffffff", "fill-opacity": 0.022 },
        },
        {
          id: "basin-glow",
          type: "line",
          source: "basin",
          filter: ["==", ["get", "role"], "outline"],
          paint: {
            "line-color": t.ice,
            "line-width": ["interpolate", ["linear"], ["zoom"], 3, 3, 8, 10],
            "line-blur": ["interpolate", ["linear"], ["zoom"], 3, 3, 8, 9],
            "line-opacity": 0.12,
          },
        },
        {
          id: "basin-line",
          type: "line",
          source: "basin",
          filter: ["==", ["get", "role"], "outline"],
          layout: { "line-join": "round", "line-cap": "round" },
          paint: {
            "line-color": t.ice,
            "line-width": ["interpolate", ["linear"], ["zoom"], 3, 0.8, 8, 1.5, 12, 2],
            "line-opacity": 0.75,
          },
        },
      ],
      hit: [],
    },
    {
      id: "nts",
      label: "NTS map sheets",
      detail: "National Topographic System 1:250,000 and 1:50,000 sheets",
      sourceId: "nts_50k",
      countStat: null,
      legend: { kind: "grid", color: COLORS.neutral },
      defaultVisible: false,
      slot: "context",
      sources: { nts: { type: "geojson", data: dataUrl("context/nts_grid.geojson") } },
      layers: [
        {
          id: "nts-250k",
          type: "line",
          source: "nts",
          filter: ["all", ["==", ["get", "role"], "sheet"], ["==", ["get", "s"], "250k"]],
          paint: { "line-color": "#ffffff", "line-opacity": 0.14, "line-width": 1 },
        },
        {
          id: "nts-50k",
          type: "line",
          source: "nts",
          minzoom: 7,
          filter: ["all", ["==", ["get", "role"], "sheet"], ["==", ["get", "s"], "50k"]],
          paint: {
            "line-color": "#ffffff",
            "line-opacity": 0.07,
            "line-width": 0.8,
            "line-dasharray": [2, 2],
          },
        },
        {
          id: "nts-label",
          type: "symbol",
          source: "nts",
          // label points (one per sheet): polygon labels would repeat once per vector tile
          filter: [
            "all",
            ["==", ["get", "role"], "label"],
            [
              "any",
              ["all", ["==", ["get", "s"], "250k"], ["<", ["zoom"], 8]],
              ["all", ["==", ["get", "s"], "50k"], [">=", ["zoom"], 8]],
            ],
          ],
          layout: {
            "text-field": ["get", "code"],
            "text-font": ["Noto Sans Regular"],
            "text-size": 10,
          },
          paint: { "text-color": "#5b6776", "text-halo-color": COLORS.ground, "text-halo-width": 1 },
        },
      ],
      hit: [],
    },
    {
      id: "deposits",
      label: "Uranium deposit footprints",
      detail: "Known deposit outlines compiled by the Saskatchewan Geological Survey",
      sourceId: "uranium_deposit_footprints",
      countStat: "m:uranium_deposit_footprints",
      legend: { kind: "fill", color: COLORS.neutral },
      defaultVisible: false,
      slot: "context",
      sources: { deposits: { type: "geojson", data: dataUrl("context/uranium_deposits.geojson") } },
      layers: [
        {
          id: "deposits-fill",
          type: "fill",
          source: "deposits",
          paint: { "fill-color": COLORS.neutral, "fill-opacity": 0.18 },
        },
        {
          id: "deposits-line",
          type: "line",
          source: "deposits",
          paint: { "line-color": COLORS.neutral, "line-width": 1, "line-opacity": 0.7 },
        },
        {
          id: "deposits-label",
          type: "symbol",
          source: "deposits",
          minzoom: 9,
          layout: {
            "text-field": ["get", "deposit"],
            "text-font": ["Noto Sans Regular"],
            "text-size": 11,
            "text-offset": [0, 1.2],
          },
          paint: { "text-color": "#8b97a6", "text-halo-color": COLORS.ground, "text-halo-width": 1.2 },
        },
      ],
      hit: ["deposits-fill"],
    },
    {
      id: "occurrences",
      label: "Uranium occurrences",
      detail: "Saskatchewan Mineral Deposit Index, uranium as a primary commodity",
      sourceId: "mineral_deposits_uranium",
      countStat: "m:uranium_occurrences",
      legend: { kind: "hollow", color: COLORS.neutral },
      defaultVisible: false,
      slot: "data",
      sources: { occurrences: { type: "geojson", data: dataUrl("context/mineral_deposits_u.geojson") } },
      layers: [
        {
          id: "occurrences",
          type: "circle",
          source: "occurrences",
          paint: {
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 4, 1.5, 10, 4],
            "circle-color": "rgba(0,0,0,0)",
            "circle-stroke-color": COLORS.neutral,
            "circle-stroke-width": hoverStroke(1, 2),
            "circle-stroke-opacity": 0.8,
          },
        },
      ],
      hit: ["occurrences"],
    },
    {
      id: "geods",
      label: "GeoDS drillholes",
      detail: "Holes compiled from assessment files (Geoscience Data System); shown from zoom 7",
      sourceId: "geods_holes",
      countStat: "m:geods_holes",
      legend: { kind: "dot", color: COLORS.geods },
      defaultVisible: true,
      slot: "data",
      sources: {
        geods: { type: "geojson", data: dataUrl("bulk/geods_holes.geojson"), buffer: 16, maxzoom: 12 },
      },
      layers: [
        {
          id: "geods-dot",
          type: "circle",
          source: "geods",
          minzoom: 7,
          filter: [
            "all",
            ["==", ["get", "rd"], 0],
            [
              "any",
              [">=", ["global-state", "yearMax"], 3000],
              // an undated hole cannot be placed in time, so the timeline hides it and the HUD counts it
              ["all", ["has", "y"], ["<=", ["get", "y"], ["global-state", "yearMax"]]],
            ],
          ],
          paint: {
            // below zoom 9 a small filled dot; from zoom 9 a ring around the compilation dot, so a hole present in
            // both datasets visibly shows both (source told apart by shape as well as colour)
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 7, 1.6, 9, 3.6, 12, 6.4, 16, 9.5],
            "circle-color": COLORS.geods,
            "circle-opacity": ["interpolate", ["linear"], ["zoom"], 7, 0, 7.6, 0.85, 8.6, 0.85, 9.4, 0],
            "circle-stroke-color": [
              "case",
              ["boolean", ["feature-state", "hover"], false],
              t.pass,
              COLORS.geods,
            ],
            "circle-stroke-width": [
              "interpolate",
              ["linear"],
              ["zoom"],
              8.6,
              [
                "case",
                ["boolean", ["feature-state", "selected"], false],
                2.5,
                ["boolean", ["feature-state", "hover"], false],
                1.5,
                0,
              ],
              9.4,
              [
                "case",
                ["boolean", ["feature-state", "selected"], false],
                2.8,
                ["boolean", ["feature-state", "hover"], false],
                2.2,
                1.3,
              ],
            ] as unknown as number,
            "circle-stroke-opacity": 0.95,
          },
        },
      ],
      hit: ["geods-dot"],
    },
    {
      id: "compilation",
      label: "Compilation collars",
      detail: "Minerals and Quaternary Drillhole Compilation, province-wide",
      sourceId: "compilation",
      countStat: "m:compilation_collars",
      legend: { kind: "dot", color: COLORS.compilation },
      defaultVisible: true,
      slot: "data",
      sources: {
        compilation: {
          type: "geojson",
          data: dataUrl("bulk/compilation_collars.geojson"),
          buffer: 16,
          maxzoom: 12,
        },
      },
      layers: [
        {
          id: "compilation-dot",
          type: "circle",
          source: "compilation",
          filter: [
            "all",
            ["==", ["get", "rd"], 0],
            ["any", ["!", ["global-state", "uraniumOnly"]], ["==", ["get", "u"], 1]],
            [
              "any",
              [">=", ["global-state", "yearMax"], 3000],
              // an undated hole cannot be placed in time, so the timeline hides it and the HUD counts it
              ["all", ["has", "y"], ["<=", ["get", "y"], ["global-state", "yearMax"]]],
            ],
          ],
          paint: {
            "circle-radius": ["interpolate", ["linear"], ["zoom"], 3, 0.9, 5, 1.3, 8, 2.2, 11, 3.6, 14, 5.5],
            "circle-color": COLORS.compilation,
            "circle-opacity": ["interpolate", ["linear"], ["zoom"], 3, 0.5, 7, 0.75, 10, 0.9],
            "circle-stroke-color": t.pass,
            "circle-stroke-width": hoverStroke(0, 1.5),
          },
        },
      ],
      hit: ["compilation-dot"],
    },
    {
      id: "reports",
      label: "Report footprints",
      detail: "Areas covered by the assessment files read by this demo",
      sourceId: null,
      countStat: "m:files_read",
      legend: { kind: "line", color: t.pass },
      defaultVisible: true,
      slot: "context",
      sources: {
        "rep-footprints": repSources["rep-footprints"],
        "rep-mask": repSources["rep-mask"],
      } as Record<string, SourceSpecification>,
      layers: REPORT_CONTEXT_LAYERS,
      hit: ["fp-fill"],
    },
    {
      id: "readHoles",
      label: "Holes read from reports",
      detail: "Colour and shape show extraction status: pass, flag, miss",
      sourceId: null,
      countStat: null,
      legend: { kind: "dot", color: t.pass },
      defaultVisible: true,
      slot: "data",
      sources: { "rep-holes": repSources["rep-holes"] } as Record<string, SourceSpecification>,
      layers: REPORT_DATA_LAYERS,
      hit: ["rh-core"],
    },
    {
      id: "datumField",
      label: "NAD27 to NAD83 shift field",
      detail:
        "Arrows show where a NAD27 coordinate lands in NAD83 (computed with PROJ and the NRCan NTv2 grid)",
      sourceId: "ntv2_grid",
      countStat: null,
      legend: { kind: "line", color: t.ice },
      defaultVisible: false,
      slot: "data",
      sources: { "datum-arrows": { type: "geojson", data: EMPTY_FC } } as Record<string, SourceSpecification>,
      layers: [
        {
          id: "datum-arrows",
          type: "line",
          source: "datum-arrows",
          layout: { "line-cap": "round", "line-join": "round" },
          paint: {
            "line-color": t.ice,
            "line-opacity": 0.5,
            "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.8, 10, 1.2],
          },
        },
      ],
      hit: [],
    },
  ];
}

/** Global-state defaults used by filters (timeline, uranium-only, intro outline draw). */
export const GLOBAL_STATE_DEFAULTS = {
  yearMax: 3000,
  uraniumOnly: false,
  /** Which score the cell layer draws: "c" criteria, "l" learned, "e" effort, "d" learned minus effort. */
  scoreKey: "c",
} as const;

/** Hit-test priority for hover and click: first match wins. */
export const HIT_PRIORITY: string[] = [
  "rh-core",
  "prospect-cell",
  "prospect-gap",
  "compilation-dot",
  "geods-dot",
  "occurrences",
  "deposits-fill",
  "fp-fill",
];

/**
 * The same groups, with every source the tile manifest covers swapped for its PMTiles archive. Each layer on a
 * tiled source gets `source-layer` (the archive names its layer after the source id), and the source leaves the
 * lazy list because a tile source is fetched by view already. Groups the manifest does not cover are returned
 * as they are, so a partial manifest degrades to GeoJSON for the rest rather than to nothing.
 */
export function withTiles(
  groups: LayerGroup[],
  tiles: TilesManifest | null,
  origin: string = typeof window === "undefined" ? "" : window.location.origin,
): LayerGroup[] {
  if (!tiles) return groups;
  return groups.map((g) => {
    const entries = Object.entries(tiles.tiles).filter(([id]) => id in g.sources);
    if (!entries.length) return g;
    const tiled = new Map(entries);
    const sources: Record<string, SourceSpecification> = { ...g.sources };
    for (const [id, entry] of tiled) sources[id] = tileSource(entry, origin);
    const layers = g.layers.map((l) => {
      const entry = "source" in l && typeof l.source === "string" ? tiled.get(l.source) : undefined;
      return entry ? ({ ...l, "source-layer": entry.layer } as LayerSpecification) : l;
    });
    const lazy = Object.fromEntries(Object.entries(g.lazy ?? {}).filter(([id]) => !tiled.has(id)));
    return { ...g, sources, layers, lazy: Object.keys(lazy).length ? lazy : undefined };
  });
}
