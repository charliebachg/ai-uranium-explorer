import type { Fmt, Val } from "@/data/contract";

/**
 * The only module allowed to turn numbers into display text (enforced by tests/unit/sourceScan.test.ts).
 * Extracted values are never re-formatted: they display exactly as printed.
 */

const nf = {
  int: new Intl.NumberFormat("en-CA", { maximumFractionDigits: 0 }),
  year: new Intl.NumberFormat("en-CA", { maximumFractionDigits: 0, useGrouping: false }),
  m1: new Intl.NumberFormat("en-CA", { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
  m2: new Intl.NumberFormat("en-CA", { minimumFractionDigits: 2, maximumFractionDigits: 2 }),
  deg1: new Intl.NumberFormat("en-CA", { minimumFractionDigits: 1, maximumFractionDigits: 1 }),
  deg5: new Intl.NumberFormat("en-CA", { minimumFractionDigits: 5, maximumFractionDigits: 5 }),
  pct1: new Intl.NumberFormat("en-CA", {
    style: "percent",
    minimumFractionDigits: 1,
    maximumFractionDigits: 1,
  }),
  ratio3: new Intl.NumberFormat("en-CA", { minimumFractionDigits: 3, maximumFractionDigits: 3 }),
};

export function formatNumber(value: number, fmt: Fmt): string {
  if (fmt === "text") return String(value);
  return nf[fmt].format(value);
}

/** Display text for a stored value (without its unit). */
export function formatVal(v: Val): string {
  if (v.kind === "extracted") return v.as_printed ?? "not printed";
  if (v.value === null) return "none";
  if (typeof v.value === "string") return v.value;
  // a year is a year whatever fmt it was minted with: "2007", never "2,007 year"
  if (v.unit === "year") return formatNumber(v.value, "year");
  return formatNumber(v.value, v.fmt ?? "text");
}

/** Unit suffix for a stored value, or null. Extracted values carry the unit as printed. */
export function unitOf(v: Val): string | null {
  if (v.kind === "extracted") return v.unit_as_printed;
  if (v.unit === "year") return null;
  return v.unit ?? null;
}

// ---------- instrument readouts (cursor, scale): marked data-instrument in the DOM ----------

export function formatLatLon(lon: number, lat: number): string {
  const ns = lat >= 0 ? "N" : "S";
  const ew = lon >= 0 ? "E" : "W";
  return `${nf.deg5.format(Math.abs(lat))}° ${ns}  ${nf.deg5.format(Math.abs(lon))}° ${ew}`;
}

export function formatInstrumentMetres(m: number): string {
  return `${nf.int.format(Math.round(m))} m`;
}

export function formatZoom(z: number): string {
  return nf.deg1.format(z);
}

export function formatCount(n: number): string {
  return nf.int.format(n);
}
