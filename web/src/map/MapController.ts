import type { GeoJSONSource, MapGeoJSONFeature, MapMouseEvent } from "maplibre-gl";
import { BASEMAPS, type BasemapId } from "@/config/basemaps";
import { HIT_PRIORITY, type LayerGroup, type LayerGroupId, layerGroups, withTiles } from "@/config/layers";
import type { DatumGrid, ReportIndex } from "@/data/contract";
import { loadDatumGrid } from "@/data/loader";
import { loadReportIndex } from "@/data/reports";
import { loadTiles, type TilesManifest } from "@/data/tiles";
import { type FeatureRef, SCORE_KEY, useStore } from "@/state/store";
import { arrowField, arrowScaleForZoom } from "./layers/datum";
import { EMPTY_FC, type ReportMapData, reportMapData } from "./layers/reports";
import { type MapLibreMap, maplibregl } from "./maplibre";
import { composeStyle } from "./style/composeStyle";

const DATASET_BY_SOURCE: Record<string, FeatureRef["dataset"]> = {
  compilation: "compilation",
  geods: "geods",
  occurrences: "occurrences",
  deposits: "deposits",
  "rep-holes": "readHole",
  "rep-footprints": "footprint",
  cells: "cell",
};

/** yearMax sentinel meaning "no year filter" (matches GLOBAL_STATE_DEFAULTS.yearMax). */
const ALL_YEARS = 3000;

/** Screen space taken by floating panels; camera fits keep data clear of them. */
function panelPadding() {
  const s = useStore.getState();
  const right = s.report || s.selected ? 480 : 40;
  const left = s.ui.rail ? 320 : 80;
  return { top: 110, bottom: 60, left, right };
}

/**
 * Owns the maplibre map. React never touches maplibre directly: components call controller methods and read
 * the store; the controller writes camera, hover, selection and cursor back into the store.
 */
export class MapController {
  readonly map: MapLibreMap;
  private groups: LayerGroup[] = layerGroups();
  /** The tile manifest once it has loaded; null means GeoJSON sources (the fallback). */
  private tiles: TilesManifest | null = null;
  /** The report data the groups were last built from, so a theme change can rebuild them without losing it. */
  private reportData: ReportMapData = { footprints: EMPTY_FC, holes: EMPTY_FC, mask: EMPTY_FC };
  private hovered: { source: string; sourceLayer?: string; id: number } | null = null;
  private selectedRefs: { source: string; sourceLayer?: string; id: number }[] = [];
  private pendingMove: MapMouseEvent | null = null;
  private raf = 0;
  private unsubs: Array<() => void> = [];
  private styleToken = 0;
  private reportIndex: ReportIndex | null = null;
  private footprintIds = new Map<string, number>();
  private holeIds = new Map<string, number>();
  private selectionMarker: InstanceType<typeof maplibregl.Marker> | null = null;
  private datumGrid: DatumGrid | null = null;
  private arrowExaggeration = 1;

  constructor(container: HTMLElement) {
    const s = useStore.getState();
    this.map = new maplibregl.Map({
      container,
      style: {
        version: 8,
        sources: {},
        layers: [{ id: "bg", type: "background", paint: { "background-color": "#0b0f14" } }],
      },
      center: s.camera.center,
      zoom: s.camera.zoom,
      bearing: s.camera.bearing,
      pitch: s.camera.pitch,
      maxPitch: 70,
      attributionControl: { compact: true },
      canvasContextAttributes: { antialias: true },
      fadeDuration: 150,
    });
    this.map.addControl(new maplibregl.NavigationControl({ visualizePitch: true }), "bottom-right");
    // the top-right corner is the one clear of the rails, the legend and the cursor readout
    this.map.addControl(new maplibregl.ScaleControl({ unit: "metric", maxWidth: 110 }), "top-right");

    this.map.on("error", (e) => {
      const msg = String(e.error?.message ?? e.error ?? "map error");
      // tile 404s at the edge of coverage are normal; surface everything else
      if (!/404|Failed to fetch/.test(msg)) console.warn("[map]", msg);
    });
    this.map.on("moveend", () => {
      const c = this.map.getCenter();
      useStore.getState().setCamera({
        center: [c.lng, c.lat],
        zoom: this.map.getZoom(),
        bearing: this.map.getBearing(),
        pitch: this.map.getPitch(),
      });
    });
    this.map.on("mousemove", (e) => {
      this.pendingMove = e;
      if (!this.raf) this.raf = requestAnimationFrame(() => this.flushMove());
    });
    this.map.on("mouseout", () => {
      useStore.getState().setCursor(null);
      this.setHover(null);
    });
    this.map.on("click", (e) => this.onClick(e));
    this.map.on("moveend", () => this.refreshArrows());
    this.map.on("style.load", () => this.syncFeatureState());
    // compact attribution stays collapsed until the (i) button is used; licences also live in the dialog
    container.addEventListener("click", (e) => {
      const btn = (e.target as HTMLElement).closest(".maplibregl-ctrl-attrib-button");
      btn?.closest(".maplibregl-ctrl-attrib")?.classList.toggle("ue-user-open");
    });

    // tiles, when the archives exist: the groups are rebuilt on tile sources and the style re-applied once
    void loadTiles().then((tiles) => {
      if (!tiles) return;
      this.tiles = tiles;
      this.groups = withTiles(layerGroups(this.reportData, useStore.getState().theme), tiles);
      void this.applyStyle(useStore.getState().basemap);
    });
    this.unsubs.push(
      useStore.subscribe(
        (st) => st.basemap,
        (b) => void this.applyStyle(b),
      ),
      useStore.subscribe(
        (st) => st.visible.datumField,
        () => this.refreshArrows(),
      ),
      useStore.subscribe(
        (st) => st.visible,
        (v, prev) => {
          for (const g of this.groups) if (v[g.id] !== prev[g.id]) this.setGroupVisible(g.id, v[g.id]);
        },
      ),
      useStore.subscribe(
        (st) => st.uraniumOnly,
        (u) => this.map.setGlobalStateProperty("uraniumOnly", u),
      ),
      useStore.subscribe(
        (st) => st.timeline.yearMax,
        (y) => this.map.setGlobalStateProperty("yearMax", y ?? ALL_YEARS),
      ),
      useStore.subscribe(
        (st) => st.theme,
        (theme) => {
          this.groups = withTiles(layerGroups(this.reportData, theme), this.tiles);
          void this.applyStyle(useStore.getState().basemap);
        },
      ),
      useStore.subscribe(
        (st) => st.scoreModel,
        (m) => this.map.setGlobalStateProperty("scoreKey", SCORE_KEY[m]),
      ),
      useStore.subscribe(
        (st) => [st.selected, st.report, st.hole] as const,
        () => this.applySelection(),
        { equalityFn: (a, b) => a.every((v, i) => v === b[i]) },
      ),
      useStore.subscribe(
        (st) => st.report,
        (r) => {
          this.refreshReportData();
          if (r) this.fitReport(r);
        },
      ),
      useStore.subscribe(
        (st) => [st.report, st.hole] as const,
        ([r, h]) => {
          if (r && h) this.flyToHole(r, h);
        },
        { equalityFn: (a, b) => a[0] === b[0] && a[1] === b[1] },
      ),
    );

    void this.applyStyle(s.basemap);
    loadDatumGrid().then(
      (g) => {
        this.datumGrid = g;
        this.refreshArrows();
      },
      () => undefined,
    );
    loadReportIndex().then(
      (idx) => {
        this.reportIndex = idx;
        this.refreshReportData();
        const st = useStore.getState();
        if (st.report) this.fitReport(st.report, true);
      },
      (err) => useStore.getState().setMapError(`Report index could not be loaded: ${String(err)}`),
    );
  }

  destroy(): void {
    for (const u of this.unsubs) u();
    cancelAnimationFrame(this.raf);
    this.map.remove();
  }

  async applyStyle(basemapId: BasemapId): Promise<void> {
    const token = ++this.styleToken;
    const def = BASEMAPS.find((b) => b.id === basemapId) ?? BASEMAPS[0];
    if (!def) return;
    const st = useStore.getState();
    let base: Awaited<ReturnType<typeof def.load>>;
    try {
      base = await withTimeout(def.load(), 4000);
    } catch (err) {
      st.setMapError(`Basemap "${def.label}" unavailable (${String(err)}); showing data layers only`);
      if (def.id !== "none") return this.applyStyle("none");
      return;
    }
    if (token !== this.styleToken) return;
    const style = composeStyle({
      base,
      basemap: def,
      groups: this.groups,
      visible: st.visible,
      // carried through a basemap swap, so the timeline and the uranium filter survive setStyle
      globalState: { uraniumOnly: st.uraniumOnly, yearMax: st.timeline.yearMax ?? ALL_YEARS },
    });
    this.map.setStyle(style, { diff: true });
    this.map.once("idle", () => {
      if (token === this.styleToken) st.setMapReady(true);
    });
  }

  /** Source ids whose deferred data has already been requested, so it is fetched once per page load. */
  private loaded = new Set<string>();

  setGroupVisible(id: LayerGroupId, on: boolean): void {
    const g = this.groups.find((x) => x.id === id);
    if (!g) return;
    if (on) this.loadDeferred(g);
    for (const l of g.layers) {
      if (this.map.getLayer(l.id)) this.map.setLayoutProperty(l.id, "visibility", on ? "visible" : "none");
    }
  }

  /** Fetch an evidence layer's data the first time it is asked for. */
  private loadDeferred(g: LayerGroup): void {
    for (const [sourceId, url] of Object.entries(g.lazy ?? {})) {
      if (this.loaded.has(sourceId)) continue;
      const source = this.map.getSource(sourceId) as GeoJSONSource | undefined;
      if (!source?.setData) continue;
      this.loaded.add(sourceId);
      source.setData(url);
    }
  }

  flyTo(lngLat: [number, number], zoom?: number): void {
    const opts = { center: lngLat, zoom: zoom ?? Math.max(this.map.getZoom(), 11), essential: true };
    if (reducedMotion()) this.map.jumpTo(opts);
    else this.map.flyTo({ ...opts, duration: 1400, curve: 1.5, padding: panelPadding() });
  }

  /** Camera move used by the tour and the command palette; padding keeps the target clear of open panels. */
  flyToCamera(
    cam: { center: [number, number]; zoom: number; bearing?: number; pitch?: number },
    opts: { duration?: number; instant?: boolean } = {},
  ): void {
    const target = { ...cam, bearing: cam.bearing ?? 0, pitch: cam.pitch ?? 0 };
    if (opts.instant || reducedMotion()) this.map.jumpTo(target);
    else
      this.map.flyTo({
        ...target,
        essential: true,
        duration: opts.duration ?? 1600,
        curve: 1.4,
        padding: panelPadding(),
      });
  }

  /** Intro: start out on the globe and fly down to the province. Resolves when the camera stops. */
  introFlight(to: { center: [number, number]; zoom: number }, duration = 4600): Promise<void> {
    const start = { center: [-96, 46] as [number, number], zoom: 2.2, bearing: 0, pitch: 0 };
    this.map.jumpTo(start);
    if (reducedMotion()) {
      this.map.jumpTo(to);
      return Promise.resolve();
    }
    return new Promise((resolve) => {
      this.map.once("moveend", () => resolve());
      this.map.flyTo({ ...to, bearing: 0, pitch: 0, duration, curve: 1.25, essential: true });
    });
  }

  /** Tour: pick a provincial collar near the centre of the view, so "click a hole" has something to click. */
  selectNearestBulk(): FeatureRef | null {
    const layers = ["compilation-dot", "geods-dot"].filter((id) => {
      const l = this.map.getLayer(id);
      return l && this.map.getLayoutProperty(id, "visibility") !== "none";
    });
    if (!layers.length) return null;
    const canvas = this.map.getCanvas();
    const c = { x: canvas.clientWidth * 0.55, y: canvas.clientHeight * 0.5 };
    const feats = this.map.queryRenderedFeatures({ layers });
    let best: MapGeoJSONFeature | null = null;
    let bestD = Number.POSITIVE_INFINITY;
    for (const f of feats) {
      if (f.geometry.type !== "Point") continue;
      const p = this.map.project(f.geometry.coordinates as [number, number]);
      const d = Math.hypot(p.x - c.x, p.y - c.y);
      if (d < bestD) {
        bestD = d;
        best = f;
      }
    }
    const ref = best ? this.toRef(best) : null;
    if (ref) useStore.getState().select(ref);
    return ref;
  }

  /** Metres per screen pixel at a latitude, for true-to-scale vectors and the lens. */
  metresPerPixel(lat: number): number {
    return (156543.03392 * Math.cos((lat * Math.PI) / 180)) / 2 ** this.map.getZoom();
  }

  arrowExaggerationFactor(): number {
    return this.arrowExaggeration;
  }

  private refreshArrows(): void {
    const src = this.map.getSource("datum-arrows") as GeoJSONSource | undefined;
    if (!src || !this.datumGrid) return;
    if (!useStore.getState().visible.datumField) {
      src.setData({ type: "FeatureCollection", features: [] });
      return;
    }
    const lat = this.map.getCenter().lat;
    const { step, scale, exaggeration } = arrowScaleForZoom(this.map.getZoom(), this.metresPerPixel(lat));
    this.arrowExaggeration = exaggeration;
    useStore.getState().setArrowExaggeration(exaggeration);
    src.setData(arrowField(this.datumGrid, step, scale));
  }

  // ---------- reports ----------

  private refreshReportData(): void {
    const data = reportMapData(this.reportIndex, useStore.getState().report);
    this.reportData = data;
    this.groups = withTiles(layerGroups(data, useStore.getState().theme), this.tiles);
    this.footprintIds.clear();
    this.holeIds.clear();
    for (const f of data.footprints.features) this.footprintIds.set(String(f.properties?.file), Number(f.id));
    for (const f of data.holes.features)
      this.holeIds.set(`${f.properties?.file}::${f.properties?.hole}`, Number(f.id));
    const set = (id: string, fc: GeoJSON.FeatureCollection) =>
      (this.map.getSource(id) as GeoJSONSource | undefined)?.setData(fc);
    set("rep-footprints", data.footprints);
    set("rep-holes", data.holes);
    set("rep-mask", data.mask);
    this.applySelection();
  }

  fitReport(file: string, instant = false): void {
    const r = this.reportIndex?.reports.find((x) => x.file_num === file);
    if (!r) return;
    const ring = r.footprint.coordinates[0] ?? [];
    if (!ring.length) return;
    const lons = ring.map((c) => c[0]);
    const lats = ring.map((c) => c[1]);
    const bounds: [[number, number], [number, number]] = [
      [Math.min(...lons), Math.min(...lats)],
      [Math.max(...lons), Math.max(...lats)],
    ];
    this.map.fitBounds(bounds, {
      padding: panelPadding(),
      pitch: 0,
      maxZoom: 13,
      duration: instant || reducedMotion() ? 0 : 1600,
      essential: true,
    });
  }

  private flyToHole(file: string, hole: string): void {
    const r = this.reportIndex?.reports.find((x) => x.file_num === file);
    const h = r?.holes.find((x) => x.hole_id === hole);
    if (h?.lonlat) this.flyTo(h.lonlat, Math.max(this.map.getZoom(), 13));
  }

  // ---------- hover and selection ----------

  private hitLayers(): string[] {
    return HIT_PRIORITY.filter((id) => {
      const l = this.map.getLayer(id);
      return l && this.map.getLayoutProperty(id, "visibility") !== "none";
    });
  }

  private pick(point: { x: number; y: number }): MapGeoJSONFeature | null {
    const layers = this.hitLayers();
    if (!layers.length) return null;
    const r = 6;
    const feats = this.map.queryRenderedFeatures(
      [
        [point.x - r, point.y - r],
        [point.x + r, point.y + r],
      ],
      { layers },
    );
    if (!feats.length) return null;
    const rank = (f: MapGeoJSONFeature) => HIT_PRIORITY.indexOf(f.layer.id);
    const dist = (f: MapGeoJSONFeature) => {
      if (f.geometry.type !== "Point") return 0;
      const p = this.map.project(f.geometry.coordinates as [number, number]);
      return Math.hypot(p.x - point.x, p.y - point.y);
    };
    feats.sort((a, b) => rank(a) - rank(b) || dist(a) - dist(b));
    return feats[0] ?? null;
  }

  private flushMove(): void {
    this.raf = 0;
    const e = this.pendingMove;
    if (!e) return;
    useStore.getState().setCursor({ lng: e.lngLat.lng, lat: e.lngLat.lat });
    const f = this.pick(e.point);
    this.map.getCanvas().style.cursor = f ? "pointer" : "";
    this.setHover(f, [e.point.x, e.point.y]);
  }

  private toRef(f: MapGeoJSONFeature, at?: [number, number]): FeatureRef | null {
    const dataset = DATASET_BY_SOURCE[f.source];
    if (!dataset || typeof f.id !== "number") return null;
    const coords: [number, number] =
      f.geometry.type === "Point" ? (f.geometry.coordinates as [number, number]) : (at ?? [0, 0]);
    return { dataset, id: f.id, props: { ...f.properties }, lngLat: coords };
  }

  private setHover(f: MapGeoJSONFeature | null, point?: [number, number]): void {
    const next =
      f && typeof f.id === "number"
        ? { source: f.source, ...(f.sourceLayer ? { sourceLayer: f.sourceLayer } : {}), id: f.id }
        : null;
    const same = next && this.hovered && next.source === this.hovered.source && next.id === this.hovered.id;
    if (!same) {
      if (this.hovered && this.map.getSource(this.hovered.source)) {
        this.map.setFeatureState(this.hovered, { hover: false });
      }
      if (next) this.map.setFeatureState(next, { hover: true });
      this.hovered = next;
    }
    const ref = f ? this.toRef(f) : null;
    useStore.getState().setHover(ref && point ? { ...ref, point } : null);
  }

  private onClick(e: MapMouseEvent): void {
    const f = this.pick(e.point);
    const ref = f ? this.toRef(f, [e.lngLat.lng, e.lngLat.lat]) : null;
    const st = useStore.getState();
    if (!ref) {
      if (st.selected) st.select(null);
      else if (st.hole) st.openHole(st.report as string, null);
      return;
    }
    if (ref.dataset === "readHole") st.openHole(String(ref.props.file), String(ref.props.hole));
    else if (ref.dataset === "footprint") {
      if (st.report !== ref.props.file) st.openReport(String(ref.props.file));
    } else if (ref.dataset === "compilation" || ref.dataset === "geods" || ref.dataset === "cell")
      st.select(ref);
  }

  private applySelection(): void {
    for (const ref of this.selectedRefs) {
      if (this.map.getSource(ref.source)) this.map.setFeatureState(ref, { selected: false });
    }
    this.selectedRefs = [];
    const st = useStore.getState();
    const add = (source: string, id: number | undefined) => {
      if (id === undefined || !this.map.getSource(source)) return;
      const layer = this.tiles?.tiles[source]?.layer;
      const ref = layer ? { source, sourceLayer: layer, id } : { source, id };
      this.map.setFeatureState(ref, { selected: true });
      this.selectedRefs.push(ref);
    };
    if (st.selected) add(st.selected.dataset, st.selected.id);
    if (st.report) add("rep-footprints", this.footprintIds.get(st.report));
    if (st.report && st.hole) add("rep-holes", this.holeIds.get(`${st.report}::${st.hole}`));

    this.selectionMarker?.remove();
    this.selectionMarker = null;
    let at: [number, number] | null = null;
    // a cell is a point on the map too: the agent rail's cell gets the ring, so "centre on this cell" lands on
    // something visible among thirty thousand dots that look alike
    if (st.selected && ["compilation", "geods", "cell"].includes(st.selected.dataset))
      at = st.selected.lngLat;
    else if (st.report && st.hole) {
      const r = this.reportIndex?.reports.find((x) => x.file_num === st.report);
      at = r?.holes.find((x) => x.hole_id === st.hole)?.lonlat ?? null;
    }
    if (at) {
      const el = document.createElement("div");
      el.className = "ue-select-ring";
      this.selectionMarker = new maplibregl.Marker({ element: el, anchor: "center" })
        .setLngLat(at)
        .addTo(this.map);
    }
  }

  /** Feature-state is lost on setStyle; re-apply hover and selection from the store. */
  private syncFeatureState(): void {
    this.hovered = null;
    this.selectedRefs = [];
    this.refreshReportData();
    this.refreshArrows();
    this.map.setGlobalStateProperty("uraniumOnly", useStore.getState().uraniumOnly);
    this.map.setGlobalStateProperty("scoreKey", SCORE_KEY[useStore.getState().scoreModel]);
    // setStyle discards source data, so anything already fetched has to be asked for again
    this.loaded.clear();
    const visible = useStore.getState().visible;
    for (const g of this.groups) if (visible[g.id]) this.loadDeferred(g);
  }
}

function reducedMotion(): boolean {
  const q = new URLSearchParams(window.location.search).get("motion");
  if (q === "0") return true;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

function withTimeout<T>(p: Promise<T>, ms: number): Promise<T> {
  return new Promise((resolve, reject) => {
    const t = window.setTimeout(() => reject(new Error(`timed out after ${ms} ms`)), ms);
    p.then(
      (v) => {
        window.clearTimeout(t);
        resolve(v);
      },
      (e) => {
        window.clearTimeout(t);
        reject(e);
      },
    );
  });
}
