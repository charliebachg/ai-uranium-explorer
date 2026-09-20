import { useEffect, useRef } from "react";
import { MapController } from "./MapController";

let current: MapController | null = null;

/** The live controller (null before mount). Used by commands such as fly-to from panels. */
export function mapController(): MapController | null {
  return current;
}

export function MapView() {
  const ref = useRef<HTMLDivElement>(null);
  useEffect(() => {
    if (!ref.current) return;
    const ctl = new MapController(ref.current);
    current = ctl;
    const w = window as unknown as { __ue?: Record<string, unknown> };
    w.__ue = { ...(w.__ue ?? {}), map: ctl.map };
    return () => {
      ctl.destroy();
      if (current === ctl) current = null;
    };
  }, []);
  // maplibre-gl.css sets .maplibregl-map{position:relative}; the wrapper keeps the map full-bleed
  return (
    <div className="absolute inset-0">
      <div ref={ref} className="h-full w-full" data-testid="map" />
    </div>
  );
}
