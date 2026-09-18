import { useEffect, useState } from "react";
import type { DatumGrid } from "@/data/contract";
import { loadDatumGrid } from "@/data/loader";
import { formatInstrumentMetres } from "@/lib/format";
import { compassPoint, shiftAt } from "@/map/geo/datumGrid";
import { mapController } from "@/map/MapView";
import { useStore } from "@/state/store";

/**
 * Cursor lens: draws the local NAD27 to NAD83 shift vector. True to scale when the vector is at least 6 px long,
 * otherwise exaggerated with the factor shown on screen. Instrument readout, not evidence.
 */
const SIZE = 168;

export function DatumLens() {
  const on = useStore((s) => s.datum.lens);
  const cursor = useStore((s) => s.cursor);
  const [grid, setGrid] = useState<DatumGrid | null>(null);
  const [pos, setPos] = useState<{ x: number; y: number } | null>(null);

  useEffect(() => {
    if (on && !grid) loadDatumGrid().then(setGrid, () => setGrid(null));
  }, [on, grid]);

  useEffect(() => {
    if (!on) return;
    const move = (e: MouseEvent) => setPos({ x: e.clientX, y: e.clientY });
    window.addEventListener("mousemove", move);
    return () => window.removeEventListener("mousemove", move);
  }, [on]);

  if (!on || !grid || !cursor || !pos) return null;
  const s = shiftAt(grid, cursor.lng, cursor.lat);
  const mpp = mapController()?.metresPerPixel(cursor.lat) ?? 100;
  if (!s) return null;

  const truePx = s.dist / mpp;
  const exaggeration = truePx >= 6 ? 1 : Math.max(1, Math.round(6 / Math.max(truePx, 0.01)));
  const px = truePx * exaggeration;
  const rad = (s.bearing * Math.PI) / 180;
  const dx = Math.sin(rad) * px;
  const dy = -Math.cos(rad) * px;
  const c = SIZE / 2;

  return (
    <div
      className="pointer-events-none fixed z-30"
      style={{ left: pos.x - c, top: pos.y - c, width: SIZE, height: SIZE }}
      data-instrument="datum-lens"
    >
      <svg width={SIZE} height={SIZE} role="img" aria-label="Datum shift lens">
        <title>NAD27 to NAD83 shift at the cursor</title>
        <circle
          cx={c}
          cy={c}
          r={c - 2}
          fill="rgba(11,15,20,0.35)"
          stroke="rgba(201,214,230,0.35)"
          strokeDasharray="3 3"
        />
        <circle cx={c} cy={c} r={2.5} fill="#c9d6e6" />
        <line x1={c} y1={c} x2={c + dx} y2={c + dy} stroke="#e6f0ff" strokeWidth="1.6" />
        <circle cx={c + dx} cy={c + dy} r={3} fill="#e6f0ff" />
      </svg>
      <div className="-translate-x-1/2 absolute top-[calc(50%+14px)] left-1/2 whitespace-nowrap rounded-md bg-ground/85 px-2 py-1 text-center text-[11px] text-ink-2">
        <div className="tabular">
          {formatInstrumentMetres(s.dist)} {compassPoint(s.bearing)}
        </div>
        <div className="text-[10px] text-ink-3">
          {exaggeration > 1 ? `vector exaggerated x${exaggeration}` : "vector to scale"}
        </div>
      </div>
    </div>
  );
}
