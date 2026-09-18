import { useEffect, useRef, useState } from "react";
import { type GeoJSONSource, type MapLibreMap, maplibregl } from "@/map/maplibre";
import { inkStyle, SLOTS, TERRARIUM_ATTRIBUTION, TERRARIUM_TILES } from "@/map/style/inkStyle";

/**
 * Phase 0 spikes: prove the MapLibre 6 capabilities the design depends on, in a real browser.
 * Results are rendered in a panel and exposed on window.__spike for automated checks.
 */

type Result = { name: string; ok: boolean | null; detail: string };

declare global {
  interface Window {
    __spike?: { results: Result[]; errors: string[]; map?: MapLibreMap; done: boolean };
  }
}

const idle = (map: MapLibreMap) => new Promise<void>((r) => map.once("idle", () => r()));
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

export function SpikeApp() {
  const ref = useRef<HTMLDivElement>(null);
  const [results, setResults] = useState<Result[]>([]);

  useEffect(() => {
    if (!ref.current) return;
    const errors: string[] = [];
    const out: Result[] = [];
    const push = (r: Result) => {
      out.push(r);
      setResults([...out]);
    };
    window.__spike = { results: out, errors, done: false };

    const map = new maplibregl.Map({
      container: ref.current,
      style: inkStyle(),
      center: [-106.5, 57.8],
      zoom: 1.6,
      attributionControl: { compact: true },
      canvasContextAttributes: { antialias: true },
    });
    window.__spike.map = map;
    map.on("error", (e) => errors.push(String(e.error?.message ?? e.error ?? e)));

    const run = async () => {
      await new Promise<void>((r) => map.once("load", () => r()));

      // 1. WebGL2 and worker: vector tiles decode in the worker, so a loaded vector source proves it.
      const gl = (map as unknown as { painter?: { context?: { gl?: unknown } } }).painter?.context?.gl;
      push({
        name: "WebGL2 context",
        ok: gl instanceof WebGL2RenderingContext,
        detail: String(gl?.constructor?.name),
      });
      await idle(map);
      push({
        name: "worker: vector tiles decoded",
        ok: map.isSourceLoaded("ofm") && map.queryRenderedFeatures({ layers: ["water"] }).length > 0,
        detail: `water features rendered: ${map.queryRenderedFeatures({ layers: ["water"] }).length}`,
      });

      // 2. Projection morph as a zoom expression.
      try {
        map.setProjection({
          type: ["interpolate", ["linear"], ["zoom"], 3.5, "vertical-perspective", 5.5, "mercator"],
        });
        map.setSky({
          "sky-color": "#0b0f14",
          "horizon-color": "#1c2b3b",
          "fog-color": "#0b0f14",
          "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 3, 1, 6, 0],
        });
        await idle(map);
        push({
          name: "projection morph expression + sky",
          ok: errors.length === 0,
          detail: JSON.stringify(map.getProjection()?.type).slice(0, 80),
        });
      } catch (e) {
        push({ name: "projection morph expression + sky", ok: false, detail: String(e) });
      }

      // 3. Relief: terrarium DEM, multidirectional hillshade, monochrome color-relief.
      try {
        map.addSource("dem-hs", {
          type: "raster-dem",
          tiles: [TERRARIUM_TILES],
          encoding: "terrarium",
          tileSize: 256,
          maxzoom: 12,
          attribution: TERRARIUM_ATTRIBUTION,
        });
        map.addLayer(
          {
            id: "relief-color",
            type: "color-relief",
            source: "dem-hs",
            paint: {
              "color-relief-color": [
                "interpolate",
                ["linear"],
                ["elevation"],
                200,
                "#0b0f14",
                450,
                "#10161e",
                800,
                "#161d27",
              ],
              "color-relief-opacity": 0.9,
            },
          },
          SLOTS.relief,
        );
        map.addLayer(
          {
            id: "relief-hillshade",
            type: "hillshade",
            source: "dem-hs",
            paint: {
              "hillshade-method": "multidirectional",
              "hillshade-shadow-color": "#04060a",
              "hillshade-highlight-color": "#2b3848",
              "hillshade-exaggeration": 0.45,
            },
          },
          SLOTS.relief,
        );
        const before = errors.length;
        map.jumpTo({ center: [-105.2, 58.1], zoom: 7.2 });
        await idle(map);
        push({
          name: "terrarium hillshade (multidirectional) + color-relief",
          ok: errors.length === before && map.isSourceLoaded("dem-hs"),
          detail: `dem loaded: ${map.isSourceLoaded("dem-hs")}`,
        });
      } catch (e) {
        push({ name: "terrarium hillshade (multidirectional) + color-relief", ok: false, detail: String(e) });
      }

      // 4. global-state filter (timeline playback without setFilter churn).
      try {
        const pts = Array.from({ length: 60 }, (_, i) => ({
          type: "Feature" as const,
          id: i + 1,
          properties: { y: 1960 + (i % 60) },
          geometry: {
            type: "Point" as const,
            coordinates: [-106.5 + ((i * 37) % 30) / 10, 57.4 + ((i * 11) % 12) / 10],
          },
        }));
        map.addSource("spike-pts", { type: "geojson", data: { type: "FeatureCollection", features: pts } });
        map.addLayer(
          {
            id: "spike-pts",
            type: "circle",
            source: "spike-pts",
            filter: ["<=", ["get", "y"], ["global-state", "year"]],
            paint: {
              "circle-radius": 4,
              "circle-color": "#3987e5",
              "circle-stroke-color": [
                "case",
                ["boolean", ["feature-state", "hover"], false],
                "#e6f0ff",
                "#0b0f14",
              ],
              "circle-stroke-width": 1,
            },
          },
          SLOTS.data,
        );
        map.jumpTo({ center: [-105.0, 58.0], zoom: 6.3 });
        map.setGlobalStateProperty("year", 2019);
        await idle(map);
        const all = map.queryRenderedFeatures({ layers: ["spike-pts"] }).length;
        map.setGlobalStateProperty("year", 1989);
        await sleep(50);
        await idle(map).catch(() => undefined);
        map.triggerRepaint();
        await sleep(300);
        const half = map.queryRenderedFeatures({ layers: ["spike-pts"] }).length;
        push({
          name: "global-state filter",
          ok: all > half && half > 0,
          detail: `year<=2019: ${all} rendered, year<=1989: ${half}`,
        });
      } catch (e) {
        push({ name: "global-state filter", ok: false, detail: String(e) });
      }

      // 5. updateData animation (misread-datum toggle) and feature-state.
      try {
        const src = map.getSource("spike-pts") as GeoJSONSource;
        map.setGlobalStateProperty("year", 2100);
        const t0 = performance.now();
        let frames = 0;
        await new Promise<void>((resolve) => {
          const step = () => {
            const t = Math.min(1, (performance.now() - t0) / 800);
            src.updateData({
              update: [
                { id: 1, newGeometry: { type: "Point", coordinates: [-106.5 - 0.5 * t, 57.4 + 0.3 * t] } },
              ],
            });
            frames++;
            if (t < 1) requestAnimationFrame(step);
            else resolve();
          };
          requestAnimationFrame(step);
        });
        await idle(map);
        map.setFeatureState({ source: "spike-pts", id: 2 }, { hover: true });
        const f = map.querySourceFeatures("spike-pts").find((x) => x.id === 1);
        const c = f?.geometry.type === "Point" ? f.geometry.coordinates : null;
        const moved = !!c && Math.abs((c[0] ?? 0) - -107.0) < 0.01 && Math.abs((c[1] ?? 0) - 57.7) < 0.01;
        push({
          name: "GeoJSONSource.updateData animation + feature-state",
          ok: moved,
          detail: `${frames} frames in 800 ms; final ${c?.map((v: number) => v.toFixed(3)).join(", ")}`,
        });
      } catch (e) {
        push({ name: "GeoJSONSource.updateData animation + feature-state", ok: false, detail: String(e) });
      }

      // 6. Fly back out through the morph to confirm no errors during the transition.
      const before = errors.length;
      map.flyTo({ center: [-80, 45], zoom: 1.8, duration: 1200 });
      await new Promise<void>((r) => map.once("moveend", () => r()));
      map.flyTo({ center: [-105.5, 58.0], zoom: 6.4, pitch: 30, duration: 2500, curve: 1.6 });
      await new Promise<void>((r) => map.once("moveend", () => r()));
      await idle(map);
      push({
        name: "globe to mercator fly-in without errors",
        ok: errors.length === before,
        detail: `errors during flights: ${errors.length - before}`,
      });

      push({
        name: "map errors (total)",
        ok: errors.length === 0,
        detail: errors.slice(0, 3).join(" | ") || "none",
      });
      if (window.__spike) window.__spike.done = true;
    };
    run().catch((e) => push({ name: "spike runner", ok: false, detail: String(e) }));
    return () => map.remove();
  }, []);

  return (
    <div className="relative h-full w-full">
      {/* maplibre-gl.css sets .maplibregl-map{position:relative}, which beats layered utilities: wrap it */}
      <div className="absolute inset-0">
        <div ref={ref} className="h-full w-full" />
      </div>
      <div className="glass absolute top-4 left-4 w-[420px] rounded-xl p-4 text-[13px]">
        <div className="mb-2 font-medium tracking-wide text-ink-2 uppercase text-[11px]">
          Phase 0 map spikes
        </div>
        <ul className="space-y-1.5">
          {results.map((r) => (
            <li key={r.name} className="flex gap-2">
              <span className={r.ok ? "text-src-geods" : r.ok === false ? "text-st-miss" : "text-ink-3"}>
                {r.ok ? "PASS" : "FAIL"}
              </span>
              <span>
                <span className="text-ink">{r.name}</span>
                <span className="block text-ink-3 tabular text-[11px]">{r.detail}</span>
              </span>
            </li>
          ))}
        </ul>
      </div>
    </div>
  );
}
