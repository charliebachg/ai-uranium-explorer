import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import type { ValueId } from "@/data/contract";
import { registerValues } from "@/data/registry";
import { AnswerText } from "@/features/prospect/AnswerText";

/**
 * The answer's inline citations become value chips, whatever namespace the interface agent's tools mint them
 * under: the sensitivity (`c:sens:`) and the expert tier (`c:insight:`) beside the cell values. An id the
 * registry does not hold is dropped rather than printed as a dead reference.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CELL = "0201_0072";
const IF_MET = `c:sens:${CELL}:fault_proximity:score_if_met`;
const INSIGHT = `c:insight:${CELL}:40f37e5a6b:0`;

let root: Root | null = null;
let container: HTMLElement | null = null;

function mount(text: string): HTMLElement {
  registerValues(
    {
      [IF_MET]: {
        id: IF_MET as ValueId,
        kind: "stat",
        as_printed: null,
        value: 0.75,
        unit_as_printed: null,
        fmt: "ratio3",
      },
      [INSIGHT]: {
        id: INSIGHT as ValueId,
        kind: "stat",
        as_printed: null,
        value: 600,
        unit_as_printed: null,
        fmt: "int",
      },
    },
    { notify: false },
  );
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<AnswerText text={text} />);
  });
  return container;
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = null;
  container = null;
});

describe("AnswerText with the interface agent's ids", () => {
  it("lifts sensitivity and expert-tier ids to value chips and drops an id nobody registered", () => {
    const el = mount(
      `If the fault were measured and met the score would be 0.75 (${IF_MET}); you said 600 m (${INSIGHT}), ` +
        "and this one is dead (c:sens:0201_0072:nothing:score_if_met).",
    );
    expect(el.querySelector(`[data-vid="${IF_MET}"]`)).not.toBeNull();
    expect(el.querySelector(`[data-vid="${INSIGHT}"]`)).not.toBeNull();
    expect(el.textContent).not.toContain("c:sens:0201_0072:nothing");
    expect(el.textContent).not.toContain(`(${IF_MET})`);
    expect(el.textContent).toContain("If the fault were measured and met the score would be 0.75");
  });
});

describe("a typed number and its chip", () => {
  it("prints the cited value once: the chip replaces the same number typed before it, and nothing else", () => {
    const el = mount(
      `If the fault were measured and met the score would be 0.75 (${IF_MET}), within 5 km (${INSIGHT}).`,
    );
    const text = el.textContent ?? "";
    // 0.75 typed, then its chip 0.750: printed once, as the chip
    expect(text).toContain("would be 0.750");
    expect(text).not.toContain("0.75 0.750");
    expect(text.match(/0\.75/g)?.length).toBe(1);
    // "5 km" is not the cited 600: it stays as written, beside the chip
    expect(text).toContain("within 5 km");
    expect(el.querySelector(`[data-vid="${INSIGHT}"]`)?.textContent).toBe("600");
  });
});
