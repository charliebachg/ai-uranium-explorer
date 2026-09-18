import { useEffect, useState } from "react";
import type { DatumGrid } from "@/data/contract";
import { loadDatumGrid } from "@/data/loader";
import { formatInstrumentMetres, formatLatLon } from "@/lib/format";
import { compassPoint, shiftAt, utmZone } from "@/map/geo/datumGrid";
import { useStore } from "@/state/store";

/** Always-on instrument: cursor position, UTM zone and the local NAD27 -> NAD83 shift (interpolated). */
export function CursorReadout() {
  const cursor = useStore((s) => s.cursor);
  const [grid, setGrid] = useState<DatumGrid | null>(null);
  useEffect(() => {
    loadDatumGrid().then(setGrid, () => setGrid(null));
  }, []);

  const shift = cursor && grid ? shiftAt(grid, cursor.lng, cursor.lat) : null;

  return (
    <div
      className="glass pointer-events-auto min-w-[250px] rounded-xl px-3 py-2 text-[11.5px] shadow-xl shadow-black/30"
      data-instrument="cursor"
    >
      {cursor ? (
        <>
          <div className="tabular text-ink-2">{formatLatLon(cursor.lng, cursor.lat)}</div>
          <div className="mt-0.5 flex items-center justify-between gap-3 text-ink-3">
            <span className="tabular">UTM zone {utmZone(cursor.lng)}</span>
            {shift ? (
              <span
                className="tabular"
                title="Interpolated from a 0.1 degree grid computed with pyproj and the NRCan NTv2 grid"
              >
                NAD27 to NAD83: <span className="text-ink-2">{formatInstrumentMetres(shift.dist)}</span>{" "}
                {compassPoint(shift.bearing)}
              </span>
            ) : null}
          </div>
        </>
      ) : (
        <div className="text-ink-3">Move over the map for position and datum shift</div>
      )}
    </div>
  );
}
