import type { DatumGrid } from "@/data/contract";

/**
 * Instrument readout only: bilinear interpolation of the pipeline's NAD27 -> NAD83 shift grid at the cursor.
 * Evidential shifts (per collar) always come from the pipeline as derived values.
 */

export type Shift = { de: number; dn: number; dist: number; bearing: number };

export function shiftAt(grid: DatumGrid, lon: number, lat: number): Shift | null {
  const fx = (lon - grid.lon0) / grid.dlon;
  const fy = (lat - grid.lat0) / grid.dlat;
  if (fx < 0 || fy < 0 || fx > grid.nlon - 1 || fy > grid.nlat - 1) return null;
  const x0 = Math.min(Math.floor(fx), grid.nlon - 2);
  const y0 = Math.min(Math.floor(fy), grid.nlat - 2);
  const tx = fx - x0;
  const ty = fy - y0;
  const at = (arr: number[], i: number, j: number) => arr[i * grid.nlon + j] ?? 0;
  const lerp = (arr: number[]) =>
    at(arr, y0, x0) * (1 - tx) * (1 - ty) +
    at(arr, y0, x0 + 1) * tx * (1 - ty) +
    at(arr, y0 + 1, x0) * (1 - tx) * ty +
    at(arr, y0 + 1, x0 + 1) * tx * ty;
  const de = lerp(grid.de_m);
  const dn = lerp(grid.dn_m);
  const bearing = ((Math.atan2(de, dn) * 180) / Math.PI + 360) % 360;
  return { de, dn, dist: Math.hypot(de, dn), bearing };
}

const COMPASS = [
  "N",
  "NNE",
  "NE",
  "ENE",
  "E",
  "ESE",
  "SE",
  "SSE",
  "S",
  "SSW",
  "SW",
  "WSW",
  "W",
  "WNW",
  "NW",
  "NNW",
];

export function compassPoint(bearing: number): string {
  return COMPASS[Math.round(bearing / 22.5) % 16] ?? "N";
}

export function utmZone(lon: number): number {
  return Math.floor((lon + 180) / 6) + 1;
}
