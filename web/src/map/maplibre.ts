import * as maplibregl from "maplibre-gl";

// Worker and shared chunk are copied to public/vendor/maplibre by plugins/maplibreVendor.ts.
maplibregl.setWorkerUrl(new URL("/vendor/maplibre/maplibre-gl-worker.mjs", window.location.origin).href);

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
