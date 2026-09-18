import type { StyleSpecification } from "maplibre-gl";
import {
  INK,
  inkStyle,
  OFM_ATTRIBUTION,
  SLOTS,
  TERRARIUM_ATTRIBUTION,
  TERRARIUM_TILES,
} from "@/map/style/inkStyle";

/** The one place basemap and relief providers are configured. */

export type BasemapId = "ink" | "positron" | "ofm-dark" | "satellite" | "none";

export interface BasemapDef {
  id: BasemapId;
  label: string;
  description: string;
  load: () => Promise<StyleSpecification>;
  /** App slots are inserted before these layer ids when the style has no slot layers of its own. */
  anchors: { relief: string; context: string; data: string; top: string | null };
  requiresNetwork: boolean;
}

function restyleOfmDark(style: StyleSpecification): StyleSpecification {
  const layers = style.layers.map((l) => {
    if (l.id === "background") return { ...l, paint: { ...(l.paint ?? {}), "background-color": INK.ground } };
    if (l.id === "water" && l.type === "fill")
      return { ...l, paint: { ...(l.paint ?? {}), "fill-color": INK.water } };
    return l;
  });
  // the natural-earth shaded relief raster competes with our own hillshade
  const kept = layers.filter((l) => !("source" in l && l.source === "ne2_shaded"));
  return { ...style, layers: kept as StyleSpecification["layers"] };
}

export const BASEMAPS: BasemapDef[] = [
  {
    id: "ink",
    label: "Ink",
    description: "Lakes and shorelines, quiet labels (OpenFreeMap tiles, custom style)",
    load: async () => inkStyle(),
    anchors: { relief: SLOTS.relief, context: SLOTS.context, data: SLOTS.data, top: SLOTS.top },
    requiresNetwork: true,
  },
  {
    id: "positron",
    label: "Paper",
    description: "Pale basemap for the light theme (OpenFreeMap Positron)",
    load: async () => {
      const res = await fetch("https://tiles.openfreemap.org/styles/positron");
      if (!res.ok) throw new Error(`OpenFreeMap style HTTP ${res.status}`);
      return (await res.json()) as StyleSpecification;
    },
    anchors: { relief: "water", context: "water_name", data: "water_name", top: null },
    requiresNetwork: true,
  },
  {
    id: "ofm-dark",
    label: "Streets",
    description: "OpenFreeMap dark style with roads and places",
    load: async () => {
      const res = await fetch("https://tiles.openfreemap.org/styles/dark");
      if (!res.ok) throw new Error(`OpenFreeMap style HTTP ${res.status}`);
      return restyleOfmDark((await res.json()) as StyleSpecification);
    },
    anchors: { relief: "water", context: "water_name", data: "water_name", top: null },
    requiresNetwork: true,
  },
  {
    id: "satellite",
    label: "Satellite",
    description: "Cloudless mosaic from the same satellite the cover features come from (EOX)",
    load: async () => ({
      version: 8,
      name: "satellite",
      glyphs: "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
      sources: {
        s2: {
          type: "raster",
          tiles: [S2_CLOUDLESS.tiles],
          tileSize: 256,
          maxzoom: S2_CLOUDLESS.maxzoom,
          attribution: S2_CLOUDLESS.attribution,
        },
      },
      layers: [
        { id: "background", type: "background", paint: { "background-color": INK.ground } },
        { id: "s2", type: "raster", source: "s2", paint: { "raster-opacity": 1 } },
        // Everything drawn over this is read against it: the score ramp runs dark to pale and would be
        // illegible on bright imagery. The scrim is a readability device, not a change to the imagery, and it
        // sits under every data layer so nothing is dimmed except the photograph.
        {
          id: "s2-scrim",
          type: "background",
          paint: { "background-color": "#05080c", "background-opacity": 0.55 },
        },
      ],
    }),
    anchors: { relief: "s2", context: "s2-scrim", data: "s2-scrim", top: null },
    requiresNetwork: true,
  },
  {
    id: "none",
    label: "None",
    description: "No basemap: data layers only (works offline)",
    load: async () => ({
      version: 8,
      name: "none",
      glyphs: "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf",
      sources: {},
      layers: [{ id: "background", type: "background", paint: { "background-color": INK.ground } }],
    }),
    anchors: { relief: "", context: "", data: "", top: null },
    requiresNetwork: false,
  },
];

export const DEFAULT_BASEMAP: BasemapId = "ink";

/**
 * Sentinel-2 cloudless, the same satellite this project already computes its cover features from, so the
 * picture under the cells is genuinely one of the inputs rather than decoration.
 *
 * Licence is CC BY-NC-SA 4.0: attribution is required and commercial use is not permitted. Nothing is
 * redistributed here — the tiles are fetched by the viewer's browser, never bundled into this repository —
 * but the terms are stated in the attribution dialog so a reader is not left to guess.
 */
export const S2_CLOUDLESS = {
  tiles: "https://tiles.maps.eox.at/wmts/1.0.0/s2cloudless-2020_3857/default/g/{z}/{y}/{x}.jpg",
  maxzoom: 14,
  attribution:
    'Imagery: <a href="https://s2maps.eu" target="_blank" rel="noreferrer">Sentinel-2 cloudless 2020</a> ' +
    'by <a href="https://eox.at" target="_blank" rel="noreferrer">EOX IT Services GmbH</a>, ' +
    "contains modified Copernicus Sentinel data 2020 — " +
    '<a href="https://creativecommons.org/licenses/by-nc-sa/4.0/" target="_blank" rel="noreferrer">CC BY-NC-SA 4.0</a> ' +
    "(non-commercial)",
};

export const RELIEF = {
  tiles: TERRARIUM_TILES,
  encoding: "terrarium" as const,
  tileSize: 256,
  maxzoom: 12,
  attribution: TERRARIUM_ATTRIBUTION,
};

export const BASEMAP_ATTRIBUTION: Record<BasemapId, string> = {
  ink: OFM_ATTRIBUTION,
  positron: OFM_ATTRIBUTION,
  "ofm-dark": OFM_ATTRIBUTION,
  satellite: S2_CLOUDLESS.attribution,
  none: "",
};
