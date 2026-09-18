import { useEffect, useRef, useState } from "react";
import { V } from "@/components/values/V";
import { dataUrl } from "@/data/loader";

/**
 * A small scatter of the analysis grid: one dot per cell, shaded by how many geological features have an
 * observation there. It is deliberately not the MapLibre map and carries no basemap: nothing here ranks ground,
 * so the dots are drawn on a plain linear lon/lat frame rather than over imagery that would invite reading them
 * as a result. The cell file is several megabytes, so it is only fetched once the reader asks for it.
 */

const WIDTH = 520;
const METRES_PER_DEGREE = 111_320;

/** A single-hue ramp stepped out of the ink tokens (raised → ink-3 → ink). */
const RAMP: [number, number, number][] = [
  [27, 36, 48],
  [63, 74, 90],
  [107, 119, 133],
  [169, 180, 194],
  [232, 238, 247],
];

type Cell = { lon: number; lat: number; g: number; b: number };

/** The legend's rungs: every whole number of features a cell can carry, none to the most any cell carries. */
function steps(maxG: number): number[] {
  const out: number[] = [];
  for (let i = 0; i <= maxG; i++) out.push(i);
  return out;
}

function rampColor(t: number): string {
  const clamped = t < 0 ? 0 : t > 1 ? 1 : t;
  const last = RAMP.length - 1;
  const pos = clamped * last;
  const i = Math.min(last - 1, Math.floor(pos));
  const a = RAMP[i] ?? RAMP[0];
  const b = RAMP[i + 1] ?? RAMP[last];
  if (!a || !b) return "rgb(232 238 247)";
  const f = pos - i;
  const mix = (lo: number, hi: number) => Math.round(lo + (hi - lo) * f);
  return `rgb(${mix(a[0], b[0])} ${mix(a[1], b[1])} ${mix(a[2], b[2])})`;
}

type RawFeature = {
  geometry?: { coordinates?: unknown } | null;
  properties?: { g?: unknown; b?: unknown } | null;
};

function readCells(raw: unknown): Cell[] {
  const feats = (raw as { features?: unknown })?.features;
  if (!Array.isArray(feats)) throw new Error("coverage.geojson has no feature array");
  const cells: Cell[] = [];
  for (const f of feats as RawFeature[]) {
    const c = f.geometry?.coordinates;
    if (!Array.isArray(c)) continue;
    const lon = c[0];
    const lat = c[1];
    const g = f.properties?.g;
    const b = f.properties?.b;
    if (typeof lon !== "number" || typeof lat !== "number" || typeof g !== "number") continue;
    cells.push({ lon, lat, g, b: b === 1 ? 1 : 0 });
  }
  if (!cells.length) throw new Error("coverage.geojson holds no usable cells");
  return cells;
}

type State =
  | { status: "idle" }
  | { status: "loading" }
  | { status: "error"; message: string }
  | { status: "ready"; cells: Cell[]; maxG: number };

export function CoverageMap({
  cellMetres,
  cellSizeId,
  geoFeaturesId,
}: {
  cellMetres: number;
  cellSizeId: string;
  geoFeaturesId: string;
}) {
  const [state, setState] = useState<State>({ status: "idle" });

  const request = () => {
    if (state.status === "loading" || state.status === "ready") return;
    setState({ status: "loading" });
    fetch(dataUrl("prospect/coverage.geojson"))
      .then((res) => {
        if (!res.ok) throw new Error(`coverage.geojson: HTTP ${res.status}`);
        return res.json();
      })
      .then((raw: unknown) => {
        const cells = readCells(raw);
        let maxG = 0;
        for (const c of cells) if (c.g > maxG) maxG = c.g;
        setState({ status: "ready", cells, maxG });
      })
      .catch((e: unknown) => setState({ status: "error", message: String(e) }));
  };

  return (
    <div>
      {state.status === "idle" ? (
        <button
          type="button"
          onClick={request}
          className="rounded-lg bg-raised px-3 py-2 text-[12.5px] text-ink-2 transition-colors hover:text-ink"
          data-testid="coverage-map-request"
        >
          Show the coverage map
        </button>
      ) : null}
      {state.status === "loading" ? (
        <div
          className="animate-pulse rounded-xl bg-white/[0.04]"
          style={{ width: WIDTH, height: 420 }}
          data-testid="coverage-map-skeleton"
        />
      ) : null}
      {state.status === "error" ? <div className="text-[13px] text-st-miss">{state.message}</div> : null}
      {state.status === "ready" ? (
        <Plot cells={state.cells} maxG={state.maxG} cellMetres={cellMetres} />
      ) : null}
      <p className="mt-3 max-w-[64ch] text-[11.5px] text-ink-3">
        One dot per grid cell (<V id={cellSizeId} /> across), shaded by how many of the{" "}
        <V id={geoFeaturesId} /> geological features have an observation in that cell. It says where
        measurements exist, not what is in the ground: a pale cell has been measured often, which is not an
        assessment of it. Cells over the Athabasca sandstone are drawn solid, the buffer around the basin is
        dimmed.
      </p>
    </div>
  );
}

function Plot({ cells, maxG, cellMetres }: { cells: Cell[]; maxG: number; cellMetres: number }) {
  const ref = useRef<HTMLCanvasElement | null>(null);
  const [height, setHeight] = useState(420);

  useEffect(() => {
    const canvas = ref.current;
    if (!canvas) return;
    let lonMin = Number.POSITIVE_INFINITY;
    let lonMax = Number.NEGATIVE_INFINITY;
    let latMin = Number.POSITIVE_INFINITY;
    let latMax = Number.NEGATIVE_INFINITY;
    for (const c of cells) {
      if (c.lon < lonMin) lonMin = c.lon;
      if (c.lon > lonMax) lonMax = c.lon;
      if (c.lat < latMin) latMin = c.lat;
      if (c.lat > latMax) latMax = c.lat;
    }
    // linear lon/lat, with longitude squeezed by the cosine of the middle latitude so a square cell looks square
    const squeeze = Math.cos((((latMin + latMax) / 2) * Math.PI) / 180);
    const spanX = (lonMax - lonMin) * squeeze;
    const spanY = latMax - latMin;
    const scale = WIDTH / (spanX || 1);
    const h = Math.max(120, Math.round(spanY * scale));
    setHeight(h);

    const dpr = window.devicePixelRatio || 1;
    canvas.width = Math.round(WIDTH * dpr);
    canvas.height = Math.round(h * dpr);
    const ctx = canvas.getContext("2d");
    if (!ctx) return;
    ctx.setTransform(dpr, 0, 0, dpr, 0, 0);
    ctx.clearRect(0, 0, WIDTH, h);
    const dot = Math.max(1.6, (cellMetres / METRES_PER_DEGREE) * scale);
    // two passes so the basin sits above its buffer, and so the fill style changes once per shade
    for (const basin of [0, 1]) {
      ctx.globalAlpha = basin === 1 ? 1 : 0.45;
      for (let step = 0; step <= maxG; step++) {
        ctx.fillStyle = rampColor(maxG === 0 ? 1 : step / maxG);
        for (const c of cells) {
          if (c.b !== basin || c.g !== step) continue;
          const x = (c.lon - lonMin) * squeeze * scale;
          const y = (latMax - c.lat) * scale;
          ctx.fillRect(x, y, dot, dot);
        }
      }
    }
    ctx.globalAlpha = 1;
  }, [cells, maxG, cellMetres]);

  return (
    <figure className="m-0">
      <canvas
        ref={ref}
        role="img"
        aria-label="Grid cells shaded by how many geological features have an observation in the cell"
        className="rounded-xl border border-line bg-panel"
        style={{ width: WIDTH, height }}
        data-testid="coverage-map-canvas"
      />
      <figcaption className="mt-2 flex flex-wrap items-center gap-x-4 gap-y-2" style={{ width: WIDTH }}>
        <span className="flex items-center gap-2">
          <span className="text-[11px] text-ink-3" data-axis>
            0
          </span>
          <span className="flex overflow-hidden rounded-sm border border-line">
            {steps(maxG).map((step) => (
              <span
                key={step}
                className="block h-3 w-3.5"
                style={{ background: rampColor(maxG === 0 ? 1 : step / maxG) }}
              />
            ))}
          </span>
          <span className="text-[11px] text-ink-3" data-axis>
            {maxG}
          </span>
          <span className="text-[11px] text-ink-3">features with an observation</span>
        </span>
        <span className="flex items-center gap-1.5 text-[11px] text-ink-3">
          <span className="block size-3 rounded-sm bg-ink-2" />
          over the sandstone
          <span className="ml-2 block size-3 rounded-sm bg-ink-2 opacity-45" />
          buffer
        </span>
      </figcaption>
    </figure>
  );
}
