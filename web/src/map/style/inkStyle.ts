import type { LayerSpecification, StyleSpecification } from "maplibre-gl";

/**
 * "ink": our own dark style on OpenFreeMap's OpenMapTiles-schema vector tiles.
 * The Canadian Shield is mostly lakes, so water and shorelines carry the look; roads and labels stay quiet
 * so the data layers (always drawn above) never compete with the basemap.
 */

export const OFM_TILEJSON = "https://tiles.openfreemap.org/planet";
export const OFM_GLYPHS = "https://tiles.openfreemap.org/fonts/{fontstack}/{range}.pbf";
export const OFM_ATTRIBUTION =
  '<a href="https://openfreemap.org" target="_blank" rel="noreferrer">OpenFreeMap</a> ' +
  '<a href="https://www.openmaptiles.org/" target="_blank" rel="noreferrer">&copy; OpenMapTiles</a> ' +
  'Data from <a href="https://www.openstreetmap.org/copyright" target="_blank" rel="noreferrer">OpenStreetMap</a>';

export const TERRARIUM_TILES = "https://s3.amazonaws.com/elevation-tiles-prod/terrarium/{z}/{x}/{y}.png";
export const TERRARIUM_ATTRIBUTION =
  'Elevation: <a href="https://github.com/tilezen/joerd/blob/master/docs/attribution.md" target="_blank" rel="noreferrer">Mapzen Terrain Tiles</a> ' +
  "(contains information licensed under the Open Government Licence - Canada; SRTM and GMTED2010 courtesy of the U.S. Geological Survey)";

export const INK = {
  ground: "#0b0f14",
  water: "#05080c",
  shore: "#1c2b3b",
  waterway: "#122033",
  land: "#0d1219",
  road: "#27313d",
  boundary: "#3a4656",
  label: "#6b7785",
  labelHalo: "#0b0f14",
};

const FONT = ["Noto Sans Regular"];

/** Named slot layers: app layers are inserted before these ids, so basemap labels stay on top. */
export const SLOTS = {
  relief: "slot:relief",
  context: "slot:context",
  data: "slot:data",
  top: "slot:top",
} as const;

function slot(id: string): LayerSpecification {
  // invisible background layer used purely as an insertion anchor
  return { id, type: "background", paint: { "background-opacity": 0 }, layout: { visibility: "none" } };
}

export function inkStyle(): StyleSpecification {
  const layers: LayerSpecification[] = [
    { id: "background", type: "background", paint: { "background-color": INK.ground } },
    slot(SLOTS.relief),
    {
      id: "landcover-wood",
      type: "fill",
      source: "ofm",
      "source-layer": "landcover",
      filter: ["==", ["get", "class"], "wood"],
      paint: { "fill-color": INK.land, "fill-opacity": 0.35 },
    },
    {
      id: "water",
      type: "fill",
      source: "ofm",
      "source-layer": "water",
      filter: ["!=", ["get", "brunnel"], "tunnel"],
      paint: { "fill-color": INK.water, "fill-antialias": true },
    },
    {
      id: "water-shore",
      type: "line",
      source: "ofm",
      "source-layer": "water",
      minzoom: 5,
      paint: {
        "line-color": INK.shore,
        "line-width": ["interpolate", ["linear"], ["zoom"], 5, 0.3, 9, 0.8, 14, 1.4],
        "line-opacity": ["interpolate", ["linear"], ["zoom"], 5, 0.5, 8, 1],
      },
    },
    {
      id: "waterway",
      type: "line",
      source: "ofm",
      "source-layer": "waterway",
      minzoom: 6,
      paint: {
        "line-color": INK.waterway,
        "line-width": ["interpolate", ["linear"], ["zoom"], 6, 0.4, 12, 1.6],
      },
    },
    {
      id: "roads",
      type: "line",
      source: "ofm",
      "source-layer": "transportation",
      minzoom: 7,
      filter: [
        "match",
        ["get", "class"],
        ["motorway", "trunk", "primary", "secondary", "tertiary", "minor", "track"],
        true,
        false,
      ],
      paint: {
        "line-color": INK.road,
        "line-opacity": 0.5,
        "line-width": ["interpolate", ["linear"], ["zoom"], 7, 0.3, 12, 1.2, 15, 2.5],
      },
    },
    {
      id: "boundary-province",
      type: "line",
      source: "ofm",
      "source-layer": "boundary",
      filter: ["all", ["<=", ["get", "admin_level"], 4], ["!=", ["get", "maritime"], 1]],
      paint: {
        "line-color": INK.boundary,
        "line-width": ["interpolate", ["linear"], ["zoom"], 2, 0.4, 8, 1.1],
        "line-dasharray": [3, 2],
      },
    },
    slot(SLOTS.context),
    slot(SLOTS.data),
    {
      id: "water-name",
      type: "symbol",
      source: "ofm",
      "source-layer": "water_name",
      minzoom: 8,
      filter: ["match", ["geometry-type"], ["Point", "MultiPoint"], true, false],
      layout: {
        "text-field": ["coalesce", ["get", "name:en"], ["get", "name"]],
        "text-font": FONT,
        "text-size": 11,
      },
      paint: { "text-color": "#3f5873", "text-halo-color": INK.labelHalo, "text-halo-width": 1 },
    },
    {
      id: "place-settlement",
      type: "symbol",
      source: "ofm",
      "source-layer": "place",
      minzoom: 5,
      filter: ["match", ["get", "class"], ["city", "town", "village", "hamlet"], true, false],
      layout: {
        "text-field": ["coalesce", ["get", "name:en"], ["get", "name"]],
        "text-font": FONT,
        "text-size": ["interpolate", ["linear"], ["zoom"], 5, 10, 10, 12],
        "text-max-width": 8,
      },
      paint: { "text-color": INK.label, "text-halo-color": INK.labelHalo, "text-halo-width": 1.2 },
    },
    {
      id: "place-region",
      type: "symbol",
      source: "ofm",
      "source-layer": "place",
      maxzoom: 6,
      filter: ["match", ["get", "class"], ["state", "province", "country"], true, false],
      layout: {
        "text-field": ["coalesce", ["get", "name:en"], ["get", "name"]],
        "text-font": FONT,
        "text-size": 11,
        "text-transform": "uppercase",
        "text-letter-spacing": 0.2,
      },
      paint: { "text-color": "#4a5563", "text-halo-color": INK.labelHalo, "text-halo-width": 1 },
    },
    slot(SLOTS.top),
  ];

  return {
    version: 8,
    name: "ink",
    glyphs: OFM_GLYPHS,
    sources: {
      ofm: { type: "vector", url: OFM_TILEJSON, attribution: OFM_ATTRIBUTION },
    },
    layers,
  };
}
