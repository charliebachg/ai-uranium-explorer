import { copyFileSync, mkdirSync } from "node:fs";
import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import type { Plugin } from "vite";

/**
 * MapLibre GL 6 is ESM-only and resolves its worker at runtime from import.meta.url. Vite's dependency
 * pre-bundling moves the main module, so the relative worker URL breaks, and a production build never
 * emits the worker file. Copy the worker and its shared chunk into public/vendor/maplibre and point
 * maplibregl.setWorkerUrl at the copy (see src/map/maplibre.ts).
 */
export function maplibreVendor(): Plugin {
  const root = dirname(fileURLToPath(import.meta.url));
  const dist = resolve(root, "../node_modules/maplibre-gl/dist");
  const target = resolve(root, "../public/vendor/maplibre");
  const copy = () => {
    mkdirSync(target, { recursive: true });
    for (const f of ["maplibre-gl-worker.mjs", "maplibre-gl-shared.mjs"]) {
      copyFileSync(resolve(dist, f), resolve(target, f));
    }
  };
  return { name: "ai-uranium-explorer:maplibre-vendor", configResolved: copy };
}
