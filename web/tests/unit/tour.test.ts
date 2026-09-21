import { describe, expect, it } from "vitest";
import { WORDING } from "@/config/wording";
import { clockText, sayParts, TOUR_CLOCKS, TOUR_SECONDS, TOUR_STEPS } from "@/features/tour/steps";
import type { TourTargets } from "@/features/tour/targets";

const NO_TARGETS: TourTargets = { evidence: null, failure: null, district: null, recordedCell: null };
const WITH_CELL: TourTargets = {
  ...NO_TARGETS,
  recordedCell: {
    cellId: "0201_0072",
    lon: -103.7,
    lat: 58.2,
    askTurns: 4,
    turns: 6,
    chainId: "run:0201_0072",
    verdict: "insufficient",
    chainPublished: false,
  },
};
const sayOf = (step: (typeof TOUR_STEPS)[number], targets = NO_TARGETS) =>
  typeof step.say === "function" ? step.say(targets) : step.say;

describe("tour steps", () => {
  it("tells the acceptance story in order: data, map, scores, the null, eval, chains, the agent, the analyst", () => {
    expect(TOUR_STEPS.map((s) => s.id)).toEqual([
      "title",
      "data",
      "map",
      "scores",
      "nullmodel",
      "eval",
      "chains",
      "ask",
      "analyst",
      "limits",
    ]);
    // under eight minutes with a budget per step; the HUD shows time against it and never advances itself
    expect(TOUR_SECONDS).toBe(470);
    expect(TOUR_STEPS[0] && sayOf(TOUR_STEPS[0])).toBe(WORDING.opening);
  });

  it("derives each step's clock from the budgets, so the two cannot drift apart", () => {
    expect(TOUR_CLOCKS).toHaveLength(TOUR_STEPS.length);
    expect(TOUR_CLOCKS[0]).toBe("0:00");
    const second = TOUR_STEPS[0]?.seconds ?? 0;
    expect(TOUR_CLOCKS[1]).toBe(clockText(second));
  });

  it("prints numbers in the script only through stored value ids, with or without a recording", () => {
    for (const targets of [NO_TARGETS, WITH_CELL]) {
      for (const step of TOUR_STEPS) {
        const plain = sayParts(sayOf(step, targets))
          .filter((p) => "text" in p)
          .map((p) => (p as { text: string }).text)
          .join("");
        expect(/\d/.test(plain), `bare digits in step ${step.id}`).toBe(false);
        for (const part of sayParts(sayOf(step, targets))) {
          if ("valueId" in part) expect(part.valueId).toMatch(/^[a-z]:/);
        }
        // the line under the script is prose only: no numbers there either
        expect(/\d/.test(step.doing ?? ""), `bare digits in the note for ${step.id}`).toBe(false);
      }
    }
  });

  it("says the exchange is recorded, and what became of the analyst's chain", () => {
    const ask = TOUR_STEPS.find((s) => s.id === "ask");
    expect(ask?.doing).toMatch(/recorded/);
    const analyst = TOUR_STEPS.find((s) => s.id === "analyst");
    expect(analyst && sayOf(analyst, WITH_CELL)).toMatch(/withheld its chain/);
    expect(
      analyst &&
        sayOf(analyst, {
          ...WITH_CELL,
          recordedCell: { ...WITH_CELL.recordedCell!, chainPublished: true },
        }),
    ).toMatch(/published into the store/);
    expect(analyst && sayOf(analyst, NO_TARGETS)).not.toMatch(/chain was/);
    // the tour never puts a geologist's words into the store
    expect(analyst?.doing).toMatch(/does not take/);
  });

  it("never says the words the wording rule forbids, whatever the verdict", () => {
    // the frozen opening line is the one exception: it says what the demo does not propose
    for (const step of TOUR_STEPS.filter((s) => s.id !== "title")) {
      const text = `${sayOf(step, WITH_CELL)} ${step.doing ?? ""}`.toLowerCase();
      for (const phrase of ["high potential", "drill target", "prospective ground"])
        expect(text.includes(phrase), `${phrase} in ${step.id}`).toBe(false);
    }
  });

  it("splits a line into text and value tokens", () => {
    expect(sayParts("a {m:x} b")).toEqual([{ text: "a " }, { valueId: "m:x" }, { text: " b" }]);
    expect(sayParts("{m:x}")).toEqual([{ valueId: "m:x" }]);
    expect(sayParts("no tokens")).toEqual([{ text: "no tokens" }]);
  });

  it("formats the walkthrough clock", () => {
    expect(clockText(0)).toBe("0:00");
    expect(clockText(9.6)).toBe("0:09");
    expect(clockText(75)).toBe("1:15");
    expect(clockText(300)).toBe("5:00");
    expect(clockText(-4)).toBe("0:00");
  });
});
