import * as maplibregl from "maplibre-gl";
import { Protocol } from "pmtiles";

// Worker and shared chunk are copied to public/vendor/maplibre by plugins/maplibreVendor.ts.
maplibregl.setWorkerUrl(new URL("/vendor/maplibre/maplibre-gl-worker.mjs", window.location.origin).href);
// pmtiles:// sources: one range-requested archive per evidence layer (see data/tiles.ts)
maplibregl.addProtocol("pmtiles", new Protocol().tile);

export type {
  ExpressionSpecification,
  FilterSpecification,
  GeoJSONSource,
  LayerSpecification,
  Map as MapLibreMap,
  MapGeoJSONFeature,
  SourceSpecification,
  StyleSpecification,
} from "maplibre-gl";
export { maplibregl };
