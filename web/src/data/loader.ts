import type { z } from "zod";
import { DatumGrid, Manifest, Readiness, RecordedChat, RunSummary, YearHistogram } from "./contract";
import { registerValues } from "./registry";

/**
 * Tiny resource cache: each path is fetched and validated once; the promise is reused (works with React 19 use()).
 * Validation failures throw with the zod issues, so a bad export fails loudly instead of rendering wrong numbers.
 */

const cache = new Map<string, Promise<unknown>>();
export const DATA_ROOT = "/data";

async function fetchJson(path: string): Promise<unknown> {
  const res = await fetch(`${DATA_ROOT}/${path}`);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

export function load<S extends z.ZodTypeAny>(path: string, schema: S): Promise<z.infer<S>> {
  let p = cache.get(path) as Promise<z.infer<S>> | undefined;
  if (!p) {
    p = fetchJson(path).then((raw) => {
      const parsed = schema.safeParse(raw);
      if (!parsed.success) {
        const issues = parsed.error.issues
          .slice(0, 5)
          .map((i) => `${i.path.join(".")}: ${i.message}`)
          .join("; ");
        throw new Error(`${path} does not match the data contract: ${issues}`);
      }
      return parsed.data;
    });
    cache.set(path, p);
  }
  return p;
}

export function loadManifest(): Promise<Manifest> {
  return load("manifest.json", Manifest).then((m) => {
    registerValues(m.stats);
    return m;
  });
}

export function loadYearHistogram(): Promise<YearHistogram> {
  return load("bulk/year_histogram.json", YearHistogram).then((h) => {
    registerValues(h.values);
    return h;
  });
}

export function loadDatumGrid(): Promise<DatumGrid> {
  return load("context/datum_grid.json", DatumGrid);
}

export const dataUrl = (path: string) => `${DATA_ROOT}/${path}`;

export function loadRunSummary(): Promise<RunSummary> {
  return load("eval/run_summary.json", RunSummary).then((s) => {
    registerValues(s.values);
    return s;
  });
}

export function loadRecordedChat(): Promise<RecordedChat> {
  return load("prospect/recorded_chat.json", RecordedChat).then((r) => {
    registerValues(r.values);
    return r;
  });
}

export function loadReadiness(): Promise<Readiness> {
  return load("prospect/readiness.json", Readiness).then((r) => {
    registerValues(r.values);
    return r;
  });
}
