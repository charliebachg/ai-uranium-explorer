import { BASEMAPS, type BasemapId } from "@/config/basemaps";
import { type LayerGroupId, layerGroups } from "@/config/layers";
import { type AppState, type Camera, useStore } from "./store";

/**
 * URL state: ?c=lon,lat,zoom,bearing,pitch&b=basemap&L=cmp,gds&u=1&s=compilation:123
 * Camera, layers and basemap use replaceState (debounced); selection uses pushState so Back walks the trail.
 */

const LAYER_KEYS: Record<LayerGroupId, string> = {
  relief: "rel",
  prospect: "sco",
  conductors: "emc",
  faults: "flt",
  host: "hst",
  lakeSediment: "lsd",
  lakeWater: "lwt",
  boulders: "bld",
  surveysAir: "sva",
  surveysGround: "svg",
  basin: "basin",
  nts: "nts",
  deposits: "dep",
  occurrences: "occ",
  geods: "gds",
  compilation: "cmp",
  reports: "rep",
  readHoles: "rh",
  datumField: "dsf",
};
const KEY_TO_LAYER = Object.fromEntries(Object.entries(LAYER_KEYS).map(([k, v]) => [v, k])) as Record<
  string,
  LayerGroupId
>;

export type UrlState = {
  camera?: Camera;
  basemap?: BasemapId;
  theme?: "dark" | "light";
  visible?: Record<LayerGroupId, boolean>;
  uraniumOnly?: boolean;
  selected?: { dataset: "compilation" | "geods"; id: number };
  report?: string;
  hole?: string;
  value?: string;
  page?: number;
};

const round = (v: number, d: number) => Math.round(v * 10 ** d) / 10 ** d;

export function encodeUrl(
  s: Pick<AppState, "camera" | "basemap" | "theme" | "visible" | "uraniumOnly" | "selected"> &
    Partial<Pick<AppState, "report" | "hole" | "value" | "page">>,
): string {
  const p = new URLSearchParams();
  const { center, zoom, bearing, pitch } = s.camera;
  p.set(
    "c",
    [round(center[0], 4), round(center[1], 4), round(zoom, 2), round(bearing, 1), round(pitch, 1)].join(","),
  );
  p.set("b", s.basemap);
  if (s.theme === "light") p.set("t", "light");
  p.set(
    "L",
    (Object.keys(LAYER_KEYS) as LayerGroupId[])
      .filter((k) => s.visible[k])
      .map((k) => LAYER_KEYS[k])
      .join(","),
  );
  if (s.uraniumOnly) p.set("u", "1");
  if (s.selected && (s.selected.dataset === "compilation" || s.selected.dataset === "geods")) {
    p.set("s", `${s.selected.dataset === "compilation" ? "cmp" : "gds"}:${s.selected.id}`);
  }
  if (s.report) p.set("r", s.report);
  if (s.hole) p.set("h", s.hole);
  if (s.value) p.set("v", s.value);
  if (s.page) p.set("p", String(s.page.page));
  return `?${p.toString().replaceAll("%2C", ",").replaceAll("%3A", ":")}`;
}

export function decodeUrl(search: string): UrlState {
  const p = new URLSearchParams(search);
  const out: UrlState = {};
  const c = p.get("c")?.split(",").map(Number);
  if (c && c.length >= 3 && c.every((v) => Number.isFinite(v))) {
    out.camera = {
      center: [c[0] as number, c[1] as number],
      zoom: c[2] as number,
      bearing: c[3] ?? 0,
      pitch: c[4] ?? 0,
    };
  }
  const b = p.get("b");
  if (b && BASEMAPS.some((m) => m.id === b)) out.basemap = b as BasemapId;
  if (p.get("t") === "light") out.theme = "light";
  const L = p.get("L");
  if (L !== null) {
    const on = new Set(L.split(",").filter(Boolean));
    out.visible = Object.fromEntries(layerGroups().map((g) => [g.id, on.has(LAYER_KEYS[g.id])])) as Record<
      LayerGroupId,
      boolean
    >;
    for (const k of on) if (!(k in KEY_TO_LAYER)) delete out.visible[k as LayerGroupId];
  }
  if (p.get("u") === "1") out.uraniumOnly = true;
  const s = p.get("s")?.split(":");
  if (s && s.length === 2 && (s[0] === "cmp" || s[0] === "gds") && Number.isFinite(Number(s[1]))) {
    out.selected = { dataset: s[0] === "cmp" ? "compilation" : "geods", id: Number(s[1]) };
  }
  const r = p.get("r");
  if (r) out.report = r;
  const h = p.get("h");
  if (h && r) out.hole = h;
  const v = p.get("v");
  if (v && /^(x|d):/.test(v)) out.value = v;
  const pg = Number(p.get("p"));
  if (Number.isInteger(pg) && pg > 0) out.page = pg;
  return out;
}

/** Start syncing the store to the URL. Returns the decoded initial state (already applied). */
export function startUrlSync(): UrlState {
  const initial = decodeUrl(window.location.search);
  useStore.setState({
    ...(initial.camera ? { camera: initial.camera } : {}),
    // a link that asks for the light theme and names no basemap gets the pale one: setTheme pairs them, and
    // this path has to pair them too, or the URL lands on light chrome over a black map
    ...(initial.basemap
      ? { basemap: initial.basemap }
      : initial.theme === "light"
        ? { basemap: "positron" as BasemapId }
        : {}),
    ...(initial.theme ? { theme: initial.theme } : {}),
    ...(initial.visible ? { visible: initial.visible } : {}),
    ...(initial.uraniumOnly ? { uraniumOnly: true } : {}),
    ...(initial.report ? { report: initial.report } : {}),
    ...(initial.hole ? { hole: initial.hole } : {}),
    ...(initial.value ? { value: initial.value } : {}),
    ...(initial.report && initial.page ? { page: { file: initial.report, page: initial.page } } : {}),
  });

  let timer: number | undefined;
  useStore.subscribe(
    (s) => [s.camera, s.basemap, s.visible, s.uraniumOnly] as const,
    () => {
      window.clearTimeout(timer);
      timer = window.setTimeout(
        () => window.history.replaceState(null, "", encodeUrl(useStore.getState())),
        250,
      );
    },
    { equalityFn: (a, b) => a.every((v, i) => v === b[i]) },
  );
  // selection and evidence steps push history entries, so Back walks the trail
  useStore.subscribe(
    (s) => [s.selected, s.report, s.hole, s.value, s.page?.page] as const,
    () => window.history.pushState(null, "", encodeUrl(useStore.getState())),
    { equalityFn: (a, b) => a.every((v, i) => v === b[i]) },
  );
  window.addEventListener("popstate", () => {
    const d = decodeUrl(window.location.search);
    useStore.setState({
      report: d.report ?? null,
      hole: d.hole ?? null,
      value: d.value ?? null,
      page: d.report && d.page ? { file: d.report, page: d.page } : null,
    });
  });
  return initial;
}
