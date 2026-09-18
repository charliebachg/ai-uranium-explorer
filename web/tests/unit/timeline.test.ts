import { describe, expect, it } from "vitest";
import { YearHistogram } from "@/data/contract";
import { registerValues } from "@/data/registry";
import {
  axisTicks,
  barHeight,
  PLOT_H,
  stackedMax,
  timelineBars,
  yearAtX,
  yearFraction,
} from "@/features/timeline/scale";
import { formatNumber } from "@/lib/format";

/** A histogram in the exported shape (bins carry value ids; the counts live in the registry). */
function fixture(rows: [year: number, cmp: number, gds: number][]) {
  const stat = (id: string, value: number) => ({
    id,
    kind: "stat" as const,
    as_printed: null,
    value,
    unit_as_printed: null,
    fmt: "int" as const,
  });
  const values: Record<string, unknown> = {
    "h:undated:cmp": stat("h:undated:cmp", 7342),
    "h:undated:gds": stat("h:undated:gds", 118),
  };
  for (const [year, cmp, gds] of rows) {
    values[`h:${year}:cmp`] = stat(`h:${year}:cmp`, cmp);
    values[`h:${year}:gds`] = stat(`h:${year}:gds`, gds);
  }
  const h = YearHistogram.parse({
    bins: rows.map(([year]) => ({ year, cmp: `h:${year}:cmp`, gds: `h:${year}:gds` })),
    undated_cmp: "h:undated:cmp",
    undated_gds: "h:undated:gds",
    values,
  });
  registerValues(h.values);
  return h;
}

const H = fixture([
  [1901, 0, 0],
  [1902, 0, 0],
  [1903, 0, 0],
  [1904, 4, 1],
  [1905, 0, 0],
  [1906, 120, 80],
  [1907, 50, 0],
]);
const BARS = timelineBars(H);

describe("timelineBars", () => {
  it("drops the empty year prefix and keeps interior empty years", () => {
    expect(BARS.map((b) => b.year)).toEqual([1904, 1905, 1906, 1907]);
    expect(BARS[0]).toEqual({ year: 1904, cmp: 4, gds: 1 });
    expect(BARS[1]).toEqual({ year: 1905, cmp: 0, gds: 0 });
  });

  it("returns nothing when no year has a hole", () => {
    expect(timelineBars(fixture([[1901, 0, 0]]))).toEqual([]);
  });
});

describe("stackedMax and barHeight", () => {
  it("takes the tallest compilation-plus-GeoDS bin", () => {
    expect(stackedMax(BARS)).toBe(200);
    expect(stackedMax([])).toBe(0);
  });

  it("scales linearly: the tallest bin fills the plot and half the holes are half the height", () => {
    const max = stackedMax(BARS);
    expect(barHeight(max, max)).toBe(PLOT_H);
    expect(barHeight(max / 2, max)).toBe(PLOT_H / 2);
    expect(barHeight(0, max)).toBe(0);
    expect(barHeight(10, 0)).toBe(0);
  });
});

describe("yearAtX", () => {
  const first = 1904;
  const last = 1907;

  it("maps the ends of the plot to the first and last year", () => {
    expect(yearAtX(0, 400, first, last)).toBe(first);
    expect(yearAtX(400, 400, first, last)).toBe(last);
  });

  it("clamps outside the plot", () => {
    expect(yearAtX(-50, 400, first, last)).toBe(first);
    expect(yearAtX(1000, 400, first, last)).toBe(last);
  });

  it("puts each year under its own band", () => {
    expect(yearAtX(50, 400, first, last)).toBe(1904);
    expect(yearAtX(150, 400, first, last)).toBe(1905);
    expect(yearAtX(250, 400, first, last)).toBe(1906);
    expect(yearAtX(350, 400, first, last)).toBe(1907);
  });

  it("degenerates safely on a zero-width plot or a single year", () => {
    expect(yearAtX(120, 0, first, last)).toBe(first);
    expect(yearAtX(120, 400, first, first)).toBe(first);
  });

  it("round-trips through the middle of each band", () => {
    for (const y of [1904, 1905, 1906, 1907]) {
      expect(yearAtX(yearFraction(y, first, last) * 400, 400, first, last)).toBe(y);
    }
  });
});

describe("axisTicks", () => {
  it("keeps the first and last year and steps between them", () => {
    expect(axisTicks(1948, 2024, 20)).toEqual([1948, 1960, 1980, 2000, 2024]);
  });

  it("drops a step that would crowd an end label", () => {
    expect(axisTicks(1901, 2024, 20)).toEqual([1901, 1920, 1940, 1960, 1980, 2000, 2024]);
  });
});

describe("year formatting", () => {
  it("prints a year without a thousands separator", () => {
    expect(formatNumber(1978, "year")).toBe("1978");
    expect(formatNumber(1978, "int")).toBe("1,978");
  });
});
