import type { YearHistogram } from "@/data/contract";
import { resolveValue } from "@/data/registry";

/**
 * Geometry for the timeline HUD. Counts are read out of the value registry (the loader registers every bin)
 * and used only to measure bars: nothing here prints a number. The scale is linear against the tallest
 * stacked bin, so a bar twice as tall means twice as many holes; no sqrt, no log, no minimum bar height.
 */

/** Plot height in pixels. */
export const PLOT_H = 84;

export type TimelineBar = { year: number; cmp: number; gds: number };

function count(id: string): number {
  const v = resolveValue(id);
  return typeof v?.value === "number" ? v.value : 0;
}

/**
 * One bar per year, from the first year that has a hole through to the last bin. The empty prefix is dropped
 * (the export starts in 1901 and the first decades are empty); interior empty years are kept, because a year
 * with no holes is a fact about the datasets.
 */
export function timelineBars(h: YearHistogram): TimelineBar[] {
  const all = h.bins.map((b) => ({ year: b.year, cmp: count(b.cmp), gds: count(b.gds) }));
  const start = all.findIndex((b) => b.cmp + b.gds > 0);
  return start < 0 ? [] : all.slice(start);
}

/** Tallest stacked bin (compilation plus GeoDS); 0 when there is nothing to draw. */
export function stackedMax(bars: TimelineBar[]): number {
  return bars.reduce((m, b) => Math.max(m, b.cmp + b.gds), 0);
}

/** Pixel height of `n` holes on a plot whose tallest bin holds `max`. */
export function barHeight(n: number, max: number, plotH: number = PLOT_H): number {
  if (max <= 0 || n <= 0) return 0;
  return (n / max) * plotH;
}

/** Year under `x` pixels on a plot `width` wide: x=0 is the first year, x=width the last, outside is clamped. */
export function yearAtX(x: number, width: number, first: number, last: number): number {
  if (width <= 0 || last <= first) return first;
  const n = last - first + 1;
  const i = Math.floor((x / width) * n);
  return first + Math.min(n - 1, Math.max(0, i));
}

/** Where a year sits across the plot, 0..1, at the middle of its bar. */
export function yearFraction(year: number, first: number, last: number): number {
  const n = last - first + 1;
  if (n <= 1) return 0.5;
  return Math.min(1, Math.max(0, (year - first + 0.5) / n));
}

export function clampYear(year: number, first: number, last: number): number {
  return Math.min(last, Math.max(first, year));
}

/** Year labels for the axis: the first and last year, plus every `step` years that is not crowding them. */
export function axisTicks(first: number, last: number, step: number): number[] {
  if (last <= first) return [first];
  const out = [first];
  for (let y = Math.ceil(first / step) * step; y < last; y += step) {
    if (y - first >= step / 2 && last - y >= step / 2) out.push(y);
  }
  out.push(last);
  return out;
}
