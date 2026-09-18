import type { Fmt, Val, ValRegistry, ValueId } from "./contract";

/**
 * Global value registry. Loaders register the Val slices they fetch; <V id> resolves through here.
 * Bulk feature properties (s:cmp:<id>:<field>) are registered on demand from the feature being shown.
 */

const values = new Map<string, Val>();
const listeners = new Set<() => void>();
let version = 0;

export function registerValues(reg: ValRegistry, opts: { notify?: boolean } = {}): void {
  for (const [id, v] of Object.entries(reg)) values.set(id, v);
  if (opts.notify === false) return;
  version++;
  for (const l of listeners) l();
}

export function resolveValue(id: string): Val | undefined {
  return values.get(id);
}

export function hasValue(id: string): boolean {
  return values.has(id);
}

export function registryVersion(): number {
  return version;
}

export function subscribeRegistry(fn: () => void): () => void {
  listeners.add(fn);
  return () => listeners.delete(fn);
}

export type BulkDataset = "compilation" | "geods";

type FieldSpec = { fmt: Fmt; unit?: string; field: string };
const FIELD_FMT: Record<BulkDataset, Record<string, FieldSpec>> = {
  compilation: {
    len: { fmt: "m1", unit: "m", field: "TOTAL_DH_LENGTH_M" },
    az: { fmt: "deg1", unit: "°", field: "DH_AZIMUTH" },
    dip: { fmt: "deg1", unit: "°", field: "DH_INCLINATION" },
    y: { fmt: "text", field: "DATE_DRILLED (year)" },
  },
  geods: {
    td: { fmt: "m1", unit: "m", field: "TOTL_MSRD_DPTH_M" },
    az: { fmt: "deg1", unit: "°", field: "AZM_DEG" },
    inc: { fmt: "deg1", unit: "°", field: "INCLNTN_DEG" },
    y: { fmt: "text", field: "DRILLNG_START_DATE (year)" },
  },
};

/** Register the numeric properties of a bulk feature as source values and return their ids. */
export function registerBulkFeature(
  dataset: BulkDataset,
  featureId: number,
  props: Record<string, unknown>,
  retrievedAt: string,
): Record<string, ValueId> {
  const ns = dataset === "compilation" ? "cmp" : "gds";
  const out: Record<string, ValueId> = {};
  const add: ValRegistry = {};
  for (const [key, spec] of Object.entries(FIELD_FMT[dataset])) {
    const raw = props[key];
    if (typeof raw !== "number") continue;
    const id = `s:${ns}:${featureId}:${key}` as ValueId;
    out[key] = id;
    if (values.has(id)) continue;
    add[id] = {
      id,
      kind: "source",
      as_printed: null,
      value: key === "y" ? String(raw) : raw,
      unit_as_printed: null,
      fmt: spec.fmt,
      ...(spec.unit ? { unit: spec.unit } : {}),
      source: {
        dataset: dataset === "compilation" ? "compilation" : "geods",
        record_id: featureId,
        field: spec.field,
        retrieved_at: retrievedAt,
      },
    };
  }
  // called during render: values are immutable and read by the same render pass, so do not notify subscribers
  if (Object.keys(add).length) registerValues(add, { notify: false });
  return out;
}

/** Test and debug hook: exposes the registry for the no-unbacked-numbers browser test. */
export function exposeRegistryForTests(): void {
  const w = window as unknown as { __lr?: Record<string, unknown> };
  w.__lr = { ...(w.__lr ?? {}), resolve: (id: string) => values.get(id) ?? null, size: () => values.size };
}
