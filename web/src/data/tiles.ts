import type { SourceSpecification } from "maplibre-gl";
import { z } from "zod";
import { dataUrl } from "./loader";

/**
 * Vector tiles built by `lr export-tiles`: one PMTiles archive per evidence source, no feature dropped at any
 * zoom, the layer inside named after the map's source id. The manifest hashes each archive beside the GeoJSON
 * it came from. When the manifest is absent (a fresh clone, or a static build without tippecanoe) the map
 * falls back to fetching the GeoJSON itself, so nothing here is required for the app to work.
 */
export const TileEntry = z.object({
  file: z.string(),
  bytes: z.number().int().nonnegative(),
  sha256: z.string().length(64),
  minzoom: z.number().int(),
  maxzoom: z.number().int(),
  layer: z.string(),
  features: z.number().int().nonnegative(),
  source_geojson: z.string(),
  source_sha256: z.string().length(64),
});
export const TilesManifest = z.object({
  version: z.literal("tiles/v1"),
  built_at: z.string(),
  tippecanoe: z.string(),
  rules: z.string(),
  tiles: z.record(z.string(), TileEntry),
  skipped: z.array(z.string()),
});
export type TileEntry = z.infer<typeof TileEntry>;
export type TilesManifest = z.infer<typeof TilesManifest>;

let cached: Promise<TilesManifest | null> | null = null;

/** The manifest, or null when there are no tiles. A manifest that does not match the contract throws. */
export function loadTiles(): Promise<TilesManifest | null> {
  if (!cached) {
    cached = fetch(dataUrl("tiles/manifest.json"))
      .then(async (res) => {
        if (res.status === 404) return null;
        if (!res.ok) throw new Error(`tiles/manifest.json: HTTP ${res.status}`);
        const parsed = TilesManifest.safeParse(await res.json());
        if (!parsed.success)
          throw new Error(`tiles/manifest.json does not match the contract: ${parsed.error.message}`);
        return parsed.data;
      })
      .catch((err) => {
        // a missing or unreachable manifest is the fallback case, not an error the reader should see
        if (err instanceof TypeError) return null;
        throw err;
      });
  }
  return cached;
}

/** A MapLibre vector source over one archive, through the pmtiles:// protocol registered in map/maplibre.ts. */
export function tileSource(entry: TileEntry, origin: string): SourceSpecification {
  return {
    type: "vector",
    url: `pmtiles://${origin}${dataUrl(`tiles/${entry.file}`)}`,
    minzoom: entry.minzoom,
    maxzoom: entry.maxzoom,
  };
}
