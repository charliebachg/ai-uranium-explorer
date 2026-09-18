import { create } from "zustand";
import { subscribeWithSelector } from "zustand/middleware";
import { type BasemapId, DEFAULT_BASEMAP } from "@/config/basemaps";
import { type LayerGroupId, layerGroups, type Theme } from "@/config/layers";

export type Camera = { center: [number, number]; zoom: number; bearing: number; pitch: number };

export type ScoreModel = "criteria" | "learned" | "effort" | "difference";

/** Which property of the cell layer the map colours; the map reads this through global state. */
export const SCORE_KEY: Record<ScoreModel, string> = {
  criteria: "c",
  learned: "l",
  effort: "e",
  difference: "d",
};

export type FeatureRef = {
  dataset: "compilation" | "geods" | "occurrences" | "deposits" | "readHole" | "footprint" | "cell";
  id: number;
  props: Record<string, unknown>;
  lngLat: [number, number];
};

export const DEFAULT_CAMERA: Camera = { center: [-106.0, 57.6], zoom: 5.4, bearing: 0, pitch: 0 };

function defaultVisible(): Record<LayerGroupId, boolean> {
  return Object.fromEntries(layerGroups().map((g) => [g.id, g.defaultVisible])) as Record<
    LayerGroupId,
    boolean
  >;
}

export interface AppState {
  camera: Camera;
  basemap: BasemapId;
  theme: Theme;
  visible: Record<LayerGroupId, boolean>;
  uraniumOnly: boolean;
  scoreModel: ScoreModel;
  hover: (FeatureRef & { point: [number, number] }) | null;
  selected: FeatureRef | null;
  cursor: { lng: number; lat: number } | null;
  mapReady: boolean;
  mapError: string | null;
  ui: { rail: boolean; attribution: boolean };

  /** Evidence trail: report -> hole -> value (the value opens its page with the box highlighted). */
  report: string | null;
  hole: string | null;
  value: string | null;
  page: { file: string; page: number } | null;
  hoverInterval: string | null;
  /** Datum tools: the cursor lens and the misread-datum comparison. */
  datum: { lens: boolean; misread: boolean };
  /** Exaggeration applied to the drawn shift arrows, published by the map so the legend cannot drift. */
  arrowExaggeration: number;
  /** Timeline: yearMax null means every year (undated holes included); a year hides later and undated holes. */
  timeline: { open: boolean; yearMax: number | null; playing: boolean };
  /** Guided walkthrough: step index into TOUR_STEPS, -1 when not running. */
  tour: { step: number; startedAt: number | null };
  palette: boolean;
  intro: boolean;
  /** The walkthrough replaying a recorded conversation in the chat panel; null in ordinary use. */
  chatReplay: { cellId: string; turns: number } | null;

  setCamera: (c: Camera) => void;
  setBasemap: (b: BasemapId) => void;
  setTheme: (t: Theme) => void;
  toggleLayer: (id: LayerGroupId, on?: boolean) => void;
  setUraniumOnly: (on: boolean) => void;
  setScoreModel: (m: ScoreModel) => void;
  setHover: (h: AppState["hover"]) => void;
  select: (f: FeatureRef | null) => void;
  setCursor: (c: AppState["cursor"]) => void;
  setMapReady: (ready: boolean) => void;
  setMapError: (e: string | null) => void;
  setUi: (patch: Partial<AppState["ui"]>) => void;
  openReport: (file: string | null) => void;
  openHole: (file: string, hole: string | null) => void;
  openValue: (id: string | null, page?: { file: string; page: number }) => void;
  setPage: (page: { file: string; page: number } | null) => void;
  setHoverInterval: (id: string | null) => void;
  setDatum: (patch: Partial<AppState["datum"]>) => void;
  setArrowExaggeration: (n: number) => void;
  setTimeline: (patch: Partial<AppState["timeline"]>) => void;
  setTour: (patch: Partial<AppState["tour"]>) => void;
  setPalette: (open: boolean) => void;
  setChatReplay: (r: AppState["chatReplay"]) => void;
  setIntro: (on: boolean) => void;
  back: () => void;
}

export const useStore = create<AppState>()(
  subscribeWithSelector((set) => ({
    camera: DEFAULT_CAMERA,
    basemap: DEFAULT_BASEMAP,
    theme: "dark",
    visible: defaultVisible(),
    uraniumOnly: false,
    scoreModel: "criteria",
    hover: null,
    selected: null,
    cursor: null,
    mapReady: false,
    mapError: null,
    ui: { rail: true, attribution: false },
    report: null,
    hole: null,
    value: null,
    page: null,
    hoverInterval: null,
    datum: { lens: false, misread: false },
    arrowExaggeration: 1,
    timeline: { open: false, yearMax: null, playing: false },
    tour: { step: -1, startedAt: null },
    palette: false,
    intro: false,
    chatReplay: null,

    setCamera: (camera) => set({ camera }),
    setBasemap: (basemap) => set({ basemap }),
    // the map's own colours are baked into a maplibre style, so the theme has to reach it through a restyle;
    // a dark basemap under light chrome reads as a broken page, so the pairing moves with it
    setTheme: (theme) =>
      set((s) => ({
        theme,
        basemap:
          theme === "light" && s.basemap === "ink"
            ? "positron"
            : theme === "dark" && s.basemap === "positron"
              ? "ink"
              : s.basemap,
      })),
    toggleLayer: (id, on) => set((s) => ({ visible: { ...s.visible, [id]: on ?? !s.visible[id] } })),
    setUraniumOnly: (uraniumOnly) => set({ uraniumOnly }),
    setScoreModel: (scoreModel) => set({ scoreModel }),
    setHover: (hover) => set({ hover }),
    select: (selected) => set({ selected }),
    setCursor: (cursor) => set({ cursor }),
    setMapReady: (mapReady) => set({ mapReady }),
    setMapError: (mapError) => set({ mapError }),
    setUi: (patch) => set((s) => ({ ui: { ...s.ui, ...patch } })),
    openReport: (report) => set({ report, hole: null, value: null, page: null, selected: null }),
    openHole: (report, hole) => set({ report, hole, value: null, page: null, selected: null }),
    openValue: (value, page) => set((s) => ({ value, page: page ?? (value ? s.page : null) })),
    setPage: (page) => set({ page }),
    setHoverInterval: (hoverInterval) => set({ hoverInterval }),
    setDatum: (patch) => set((s) => ({ datum: { ...s.datum, ...patch } })),
    setArrowExaggeration: (arrowExaggeration) => set({ arrowExaggeration }),
    setTimeline: (patch) => set((s) => ({ timeline: { ...s.timeline, ...patch } })),
    setTour: (patch) => set((s) => ({ tour: { ...s.tour, ...patch } })),
    setPalette: (palette) => set({ palette }),
    setChatReplay: (chatReplay) => set({ chatReplay }),
    setIntro: (intro) => set({ intro }),
    // Esc ladder: value -> hole -> report -> nothing
    back: () =>
      set((s) => {
        if (s.value || s.page) return { value: null, page: null };
        if (s.hole) return { hole: null };
        if (s.report) return { report: null };
        return { selected: null };
      }),
  })),
);
