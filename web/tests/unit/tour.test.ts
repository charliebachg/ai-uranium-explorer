import { describe, expect, it } from "vitest";
import { WORDING } from "@/config/wording";
import { clockText, sayParts, TOUR_CLOCKS, TOUR_SECONDS, TOUR_STEPS } from "@/features/tour/steps";
import type { TourTargets } from "@/features/tour/targets";

const NO_TARGETS: TourTargets = { evidence: null, failure: null, district: null, recordedCell: null };
const sayOf = (step: (typeof TOUR_STEPS)[number], targets = NO_TARGETS) =>
  typeof step.say === "function" ? step.say(targets) : step.say;

describe("tour steps", () => {
  it("walks the whole process, in order, from the map to what it is worth", () => {
    expect(TOUR_STEPS.map((s) => s.id)).toEqual([
      "title",
      "district",
      "evidence",
      "scores",
      "nullmodel",
      "eval",
      "ask",
      "limits",
    ]);
    // report 05's script is five minutes. Dropping its single-value failure step and adding three of our own
    // — the two score views and the agent — walks the whole process instead, and costs about a minute and a half
    expect(TOUR_SECONDS).toBe(400);
    expect(TOUR_STEPS[0] && sayOf(TOUR_STEPS[0])).toBe(WORDING.opening);
  });

  it("derives each step's clock from the budgets, so the two cannot drift apart", () => {
    expect(TOUR_CLOCKS).toHaveLength(TOUR_STEPS.length);
    expect(TOUR_CLOCKS[0]).toBe("0:00");
    const second = TOUR_STEPS[0]?.seconds ?? 0;
    expect(TOUR_CLOCKS[1]).toBe(clockText(second));
  });

  it("prints numbers in the script only through stored value ids", () => {
    for (const step of TOUR_STEPS) {
      const plain = sayParts(sayOf(step))
        .filter((p) => "text" in p)
        .map((p) => (p as { text: string }).text)
        .join("");
      expect(/\d/.test(plain), `bare digits in step ${step.id}`).toBe(false);
      for (const part of sayParts(sayOf(step))) {
        if ("valueId" in part) expect(part.valueId).toMatch(/^[a-z]:/);
      }
      // the line under the script is prose only: no numbers there either
      expect(/\d/.test(step.doing ?? ""), `bare digits in the note for ${step.id}`).toBe(false);
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
