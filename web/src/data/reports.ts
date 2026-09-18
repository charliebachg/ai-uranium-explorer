import { PagesIndex, Report, ReportIndex } from "./contract";
import { registerValues } from "./registry";

/**
 * Report artifacts. In fixture mode (?fixture=1) they come from /fixtures (a hand-keyed fixture built from real
 * public pages for UI development) and every screen shows a FIXTURE watermark.
 */

export const FIXTURE_MODE =
  typeof window !== "undefined" && new URLSearchParams(window.location.search).has("fixture");
export const REPORT_ROOT = FIXTURE_MODE ? "/fixtures" : "/data";

const cache = new Map<string, Promise<unknown>>();

async function fetchParsed<T>(path: string, parse: (raw: unknown) => T): Promise<T> {
  const res = await fetch(`${REPORT_ROOT}/${path}`);
  if (res.status === 404) throw new NotFound(path);
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  // a dev server answers a missing file with index.html, so a non-JSON body means "not exported yet"
  const type = res.headers.get("content-type") ?? "";
  const body = await res.text();
  if (!type.includes("json") && !body.trimStart().startsWith("{")) throw new NotFound(path);
  return parse(JSON.parse(body));
}

export class NotFound extends Error {
  constructor(path: string) {
    super(`${path} not found`);
  }
}

function cached<T>(key: string, make: () => Promise<T>): Promise<T> {
  let p = cache.get(key) as Promise<T> | undefined;
  if (!p) {
    p = make();
    cache.set(key, p);
    p.catch(() => cache.delete(key));
  }
  return p;
}

function strict<T>(
  schema: {
    safeParse: (x: unknown) => {
      success: boolean;
      data?: T;
      error?: { issues: { path: PropertyKey[]; message: string }[] };
    };
  },
  path: string,
) {
  return (raw: unknown): T => {
    const r = schema.safeParse(raw);
    if (!r.success) {
      const issues = (r.error?.issues ?? [])
        .slice(0, 4)
        .map((i) => `${i.path.map(String).join(".")}: ${i.message}`)
        .join("; ");
      throw new Error(`${path} does not match the data contract: ${issues}`);
    }
    return r.data as T;
  };
}

/** The report index; resolves to an empty index when no reports have been exported yet. */
export function loadReportIndex(): Promise<ReportIndex> {
  const path = "reports/index.json";
  return cached(path, async () => {
    try {
      const idx = await fetchParsed(path, strict<ReportIndex>(ReportIndex, path));
      registerValues(idx.values);
      return idx;
    } catch (e) {
      if (e instanceof NotFound) return { reports: [], values: {} } as unknown as ReportIndex;
      throw e;
    }
  });
}

export function loadReport(fileNum: string): Promise<Report> {
  const path = `reports/${fileNum}/report.json`;
  return cached(path, async () => {
    const r = await fetchParsed(path, strict<Report>(Report, path));
    registerValues(r.values);
    return r;
  });
}

export function loadPages(fileNum: string): Promise<PagesIndex> {
  const path = `reports/${fileNum}/pages.json`;
  return cached(path, () => fetchParsed(path, strict<PagesIndex>(PagesIndex, path)));
}

export function pageImageUrl(rel: string | null): string | null {
  return rel ? `${REPORT_ROOT}/${rel}` : null;
}
