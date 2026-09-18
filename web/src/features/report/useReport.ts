import { useEffect, useState } from "react";
import type { Hole, PagesIndex, Report, ReportIndex } from "@/data/contract";
import { loadPages, loadReport, loadReportIndex } from "@/data/reports";

type Async<T> = { data: T | null; error: string | null; loading: boolean };

function useAsync<T>(key: string | null, load: () => Promise<T>): Async<T> {
  const [state, setState] = useState<Async<T>>({ data: null, error: null, loading: !!key });
  // biome-ignore lint/correctness/useExhaustiveDependencies: keyed by `key`; `load` closes over the same key
  useEffect(() => {
    if (!key) {
      setState({ data: null, error: null, loading: false });
      return;
    }
    let live = true;
    setState((s) => ({ ...s, loading: true, error: null }));
    load().then(
      (data) => live && setState({ data, error: null, loading: false }),
      (e: unknown) => live && setState({ data: null, error: String(e), loading: false }),
    );
    return () => {
      live = false;
    };
  }, [key]);
  return state;
}

export function useReportIndex(): Async<ReportIndex> {
  return useAsync("index", loadReportIndex);
}

export function useReport(file: string | null): Async<Report> {
  return useAsync(file, () => loadReport(file as string));
}

export function usePages(file: string | null): Async<PagesIndex> {
  return useAsync(file ? `pages:${file}` : null, () => loadPages(file as string));
}

/** All value ids of a hole in reading order (for J/K navigation). */
export function holeValueIds(hole: Hole): string[] {
  const c = hole.collar;
  const ids: (string | null)[] = [
    hole.name,
    c.easting,
    c.northing,
    c.lat,
    c.lon,
    c.grid_x,
    c.grid_y,
    c.utm_zone,
    c.datum_printed,
    c.elevation,
    c.azimuth,
    c.dip,
    c.total_depth,
  ];
  for (const a of hole.assays) {
    ids.push(a.sample_id, a.from, a.to, ...a.grades.map((g) => g.value));
  }
  for (const l of hole.lith) ids.push(l.from, l.to, l.code, l.description);
  return ids.filter((x): x is string => !!x && x.startsWith("x:"));
}
