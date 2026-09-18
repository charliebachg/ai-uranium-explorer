import type { Feature, FeatureCollection, LineString } from "geojson";
import type { DatumGrid } from "@/data/contract";
import { shiftAt } from "../geo/datumGrid";

/**
 * The NAD27 to NAD83 shift field, drawn as arrows. Geometry only: the vectors come from the pipeline's grid
 * (computed with PROJ and the NRCan NTv2 grid) and are drawn to scale where they are visible, otherwise
 * exaggerated with the factor stated on screen.
 */

const M_PER_DEG_LAT = 111_320;

export function arrowField(grid: DatumGrid, step: number, scale: number): FeatureCollection {
  const features: Feature<LineString>[] = [];
  let id = 0;
  for (let i = 0; i < grid.nlat; i += step) {
    for (let j = 0; j < grid.nlon; j += step) {
      const lat = grid.lat0 + i * grid.dlat;
      const lon = grid.lon0 + j * grid.dlon;
      const s = shiftAt(grid, lon, lat);
      if (!s) continue;
      const mPerDegLon = M_PER_DEG_LAT * Math.cos((lat * Math.PI) / 180);
      const dx = (s.de * scale) / mPerDegLon;
      const dy = (s.dn * scale) / M_PER_DEG_LAT;
      const tipLon = lon + dx;
      const tipLat = lat + dy;
      // arrow head: two short segments back from the tip at +/- 25 degrees
      const head = 0.32;
      const rot = (a: number): [number, number] => {
        const c = Math.cos(a);
        const s2 = Math.sin(a);
        const hx = -dx * head;
        const hy = -dy * head;
        return [tipLon + hx * c - hy * s2, tipLat + hx * s2 + hy * c];
      };
      id += 1;
      features.push({
        type: "Feature",
        id,
        properties: { mag: Math.round(s.dist * 10) / 10 },
        geometry: {
          type: "LineString",
          coordinates: [rot(0.44), [tipLon, tipLat], [lon, lat], [tipLon, tipLat], rot(-0.44)],
        },
      });
    }
  }
  return { type: "FeatureCollection", features };
}

/**
 * Node spacing and vector exaggeration for the current zoom. True-to-scale vectors are invisible at regional
 * zooms (about 40 m against 400 m per pixel), so length stays proportional to the shift but is exaggerated by a
 * factor the UI must display (see the map legend chip).
 */
export function arrowScaleForZoom(
  zoom: number,
  metresPerPixel: number,
  medianShiftM = 40,
): { step: number; scale: number; exaggeration: number } {
  const step = zoom >= 10 ? 1 : zoom >= 8 ? 2 : zoom >= 6.5 ? 4 : 8;
  // aim for a median arrow about 22 px long, and never claim more than 200x
  const wanted = (22 * metresPerPixel) / medianShiftM;
  const exaggeration = Math.min(Math.max(1, Math.round(wanted)), 200);
  return { step, scale: exaggeration, exaggeration };
}
