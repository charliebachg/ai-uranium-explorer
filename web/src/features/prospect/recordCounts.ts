import type { ProspectSource, ValRegistry, ValueId } from "@/data/contract";
import { registerValues } from "@/data/registry";

/**
 * The source register carries each source's record count as a plain field rather than a value id, and the page
 * is not allowed to print a bare number. So each count is registered as a stat, keyed by the source, carrying
 * the register entry it came from and the date that entry was checked. Nothing is computed here: the number is
 * the one the export wrote.
 */

export function recordCountId(key: string): ValueId {
  return `c:recs:${key}` as ValueId;
}

export function registerRecordCounts(sources: ProspectSource[]): void {
  const add: ValRegistry = {};
  for (const s of sources) {
    if (s.record_count === null) continue;
    const id = recordCountId(s.key);
    add[id] = {
      id,
      kind: "stat",
      as_printed: null,
      value: s.record_count,
      unit_as_printed: null,
      fmt: "int",
      note: `records reported by ${s.title} when the source register was checked on ${s.verified_at}`,
    };
  }
  // called from the loader's then(), before the page renders: the values are read by the render that follows
  if (Object.keys(add).length) registerValues(add, { notify: false });
}
