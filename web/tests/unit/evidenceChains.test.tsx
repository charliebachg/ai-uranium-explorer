import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { AnalystChain, CellEvidence, ReadersRun, type ValRegistry, type ValueId } from "@/data/contract";
import { registerValues } from "@/data/registry";
import { EvidencePanel } from "@/features/prospect/EvidencePanel";

/**
 * The Chains section of the evidence panel, rendered against a fixture chain shaped exactly as `serve.evidence`
 * serves one. `<V>` throws on an unbacked id under vitest, so a render that completes is itself the assertion
 * that every number the section prints resolves to a registered value.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CELL = "0201_0072";
const CONDUCTOR = `c:cell:${CELL}:d_conductor_m`;
const FAULT = `c:cell:${CELL}:d_fault_m`;

function stat(id: string, value: number, fmt: "ratio3" | "int" | "m1" | "m2", unit?: string) {
  return {
    id: id as ValueId,
    kind: "stat" as const,
    as_printed: null,
    value,
    unit_as_printed: null,
    fmt,
    unit,
  };
}

/** The values the service registers: the cited ones from the decision's values_json, and the chain's own. */
const VALUES: ValRegistry = Object.fromEntries(
  [
    stat(CONDUCTOR, 1200, "m1", "m"),
    stat(FAULT, 3500, "m1", "m"),
    stat("c:chain:ch1:final_probability", 0.61, "ratio3"),
    stat("c:chain:ch1:weighted_score", 0.58, "ratio3"),
    stat("c:chain:ch1:decision_probability", 0.61, "ratio3"),
    stat("c:chain:ch1:rounds", 2, "int"),
    stat("c:chain:ch1:cost_usd", 0.12, "m2", "USD"),
    stat("c:chain:ch1:n01:strength", 4, "int"),
    stat("c:chain:ch1:n02:strength", 3, "int"),
    stat("c:chain:ch1:n03:strength", 0, "int"),
    stat("c:chain:ch0:rounds", 1, "int"),
    stat("c:chain:ch0:n01:strength", 2, "int"),
  ].map((v) => [v.id, v]),
);

const chain: AnalystChain = {
  chain_id: "ch1",
  run_id: "20260920T120000Z-analyst",
  arm: "v1-template",
  purpose: "dashboard",
  planner: "template",
  rounds: 2,
  valid: true,
  final_verdict: "supports_closer_look",
  final_probability: 0.61,
  weighted_score: 0.58,
  weights_version: "w/v1",
  verifier_label: "supports_closer_look",
  majority_label: null,
  abstained_reason: null,
  published: true,
  created_at: "2026-09-20T12:00:00+00:00",
  cost_usd: 0.12,
  nodes: [
    {
      node_id: "n01",
      segment_id: "seg-n01",
      kind: "criterion",
      criterion: "conductor_distance",
      status: "met",
      strength: 4,
      value_ids: [CONDUCTOR],
      expert_ids: [],
      depends_on: [],
      text: "A conductor lies 1200 m from the cell centre.",
      published: true,
      problems: [],
      round: 1,
      attempt: 1,
    },
    {
      node_id: "n02",
      segment_id: "seg-n02",
      kind: "criterion",
      criterion: "fault_distance",
      status: "not_met",
      strength: 3,
      value_ids: [FAULT],
      expert_ids: [FAULT],
      depends_on: [],
      text: "The nearest fault is 3.5 km away.",
      published: true,
      problems: [],
      round: 0,
      attempt: 1,
    },
    {
      node_id: "n03",
      segment_id: "seg-n03",
      kind: "crosscheck",
      criterion: null,
      status: "unknown",
      strength: 0,
      value_ids: [CONDUCTOR, FAULT],
      expert_ids: [],
      depends_on: ["n01", "n02"],
      text: "Conductor and fault coincide within 2 km.",
      published: false,
      problems: ["ids: the number 2 is not backed by any value this node cites"],
      round: 1,
      attempt: 1,
    },
  ],
  verdicts: [
    {
      round: 0,
      valid: false,
      faulty: [{ node_id: "n01", reason: "polarity" }],
      feedback: "n01 reads a favourable feature as not met",
      candidate_label: "insufficient",
      candidate_probability: 0.5,
      rationale: null,
    },
    {
      round: 1,
      valid: true,
      faulty: [],
      feedback: null,
      candidate_label: "supports_closer_look",
      candidate_probability: 0.6,
      rationale: "the nodes agree",
    },
  ],
  decision: {
    verdict: "supports_closer_look",
    probability: 0.61,
    claims: [{ text: "The nearest conductor is 1.2 km away.", value_ids: [CONDUCTOR] }],
    unknown_criteria: ["alteration"],
    absent_criteria: ["boulder_field"],
    next_observation: "a resistivity line across the conductor",
    rationale: "one conductor, one fault, no alteration measured",
    published: true,
    problems: [],
  },
};

const record: CellEvidence = {
  cell_id: CELL,
  lon: -105.1,
  lat: 57.9,
  in_basin: true,
  parts: {},
  values: {},
  memos: [],
  chains: [chain],
  readers: [],
};

/** The record as the service served it before chains existed: no `chains` key at all. */
const { chains: _chains, ...older } = record;

let root: Root | null = null;
let container: HTMLElement | null = null;

function mount(r: CellEvidence): HTMLElement {
  registerValues(VALUES, { notify: false });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<EvidencePanel cellId={r.cell_id} state="up" record={r} error={null} loading={false} />);
  });
  return container;
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = null;
  container = null;
});

describe("the evidence contract with chains", () => {
  it("accepts a record with chains and defaults an older record to none", () => {
    expect(CellEvidence.parse(record).chains[0]?.nodes.length).toBe(3);
    expect(CellEvidence.parse(older).chains).toEqual([]);
  });

  it("holds the node statuses and the verdicts to their three levels", () => {
    const [n01] = chain.nodes;
    expect(AnalystChain.safeParse({ ...chain, nodes: [{ ...n01, status: "not met" }] }).success).toBe(false);
    expect(AnalystChain.safeParse({ ...chain, final_verdict: "maybe" }).success).toBe(false);
    expect(AnalystChain.safeParse({ ...chain, nodes: [{ ...n01, strength: 6 }] }).success).toBe(false);
    expect(
      AnalystChain.safeParse({ ...chain, decision: null, abstained_reason: "never validated" }).success,
    ).toBe(true);
  });
});

describe("the Chains section", () => {
  it("renders the verdict as words, both deciders, and the one observation", () => {
    const el = mount(record);
    const verdict = el.querySelector('[data-testid="chain-verdict"]');
    expect(verdict?.textContent).toContain("supports a closer look");
    expect(verdict?.textContent).toContain("adjudicator");
    expect(el.textContent).not.toContain("supports_closer_look");
    // the decision: the weighted sum names its weights, the adjudicator speaks in words, the observation is there
    const decision = el.querySelector('[data-testid="chain-decision"]');
    expect(decision?.textContent).toContain("Weighted sum");
    expect(decision?.textContent).toContain("w/v1");
    expect(decision?.querySelector('[data-vid="c:chain:ch1:weighted_score"]')).not.toBeNull();
    expect(decision?.querySelector('[data-vid="c:chain:ch1:decision_probability"]')).not.toBeNull();
    expect(el.querySelector('[data-testid="chain-observation"]')?.textContent).toContain(
      "a resistivity line across the conductor",
    );
    expect(decision?.textContent).toContain("alteration");
    expect(decision?.textContent).toContain("boulder_field");
    // the verifier's rounds: the invalid round names its faulty node with the reason, the valid one is marked
    const rounds = el.querySelectorAll('[data-testid="chain-round"]');
    expect(rounds.length).toBe(2);
    expect(rounds[0]?.textContent).toContain("invalid");
    expect(rounds[0]?.textContent).toContain("polarity");
    expect(rounds[0]?.textContent).toContain("insufficient evidence");
    expect(rounds[1]?.hasAttribute("data-valid")).toBe(true);
  });

  it("marks a withheld node with the gate's first objection and an expert-tier citation", () => {
    const el = mount(record);
    const withheld = el.querySelector('[data-testid="chain-node"][data-withheld]');
    expect(withheld?.getAttribute("data-node-id")).toBe("n03");
    expect(withheld?.textContent).toContain("withheld");
    expect(withheld?.textContent).toContain("the number 2 is not backed by any value this node cites");
    expect(el.querySelectorAll('[data-testid="chain-node"][data-withheld]').length).toBe(1);
    const expert = el.querySelector(
      '[data-testid="chain-node"][data-node-id="n02"] [data-testid="chain-expert"]',
    );
    expect(expert?.textContent).toContain("expert");
    // status is words in the row, not a colour alone
    expect(el.querySelector('[data-node-id="n02"]')?.textContent).toContain("not met");
    expect(el.querySelector('[data-node-id="n03"]')?.textContent).toContain("unknown");
  });

  it("prints every number in a node's text beside the value id it cites", () => {
    const el = mount(record);
    for (const node of chain.nodes) {
      const row = el.querySelector(`[data-testid="chain-node"][data-node-id="${node.node_id}"]`);
      expect(row, node.node_id).not.toBeNull();
      const text = row?.querySelector("p [data-source-text]")?.textContent ?? "";
      if (!/\d/.test(text)) continue;
      for (const id of node.value_ids) {
        expect(row?.querySelector(`[data-vid="${id}"]`), `${node.node_id} cites ${id}`).not.toBeNull();
      }
      // the strength is a stat of its own, never a bare digit
      expect(row?.querySelector(`[data-vid="c:chain:ch1:${node.node_id}:strength"]`)).not.toBeNull();
    }
  });

  it("shows the empty state in the memo section's style when no chain exists", () => {
    const el = mount({ ...record, chains: [] });
    expect(el.querySelector('[data-testid="chain-empty"]')?.textContent).toBe("None for this cell yet.");
    expect(el.querySelector('[data-testid="chain"]')).toBeNull();
  });

  it("opens the newest chain and lets the others be picked", () => {
    const olderChain: AnalystChain = {
      ...chain,
      chain_id: "ch0",
      arm: "v1-model",
      rounds: 1,
      created_at: "2026-09-19T12:00:00+00:00",
      final_verdict: "evidence_against",
      final_probability: null,
      weighted_score: null,
      weights_version: null,
      decision: null,
      cost_usd: null,
      nodes: chain.nodes.slice(0, 1).map((n) => ({ ...n, strength: 2 })),
      verdicts: [],
    };
    const el = mount({ ...record, chains: [chain, olderChain] });
    expect(el.querySelector('[data-testid="chain"]')?.getAttribute("data-chain-id")).toBe("ch1");
    const picker = el.querySelector<HTMLSelectElement>('[data-testid="chain-picker"]');
    expect(picker?.options.length).toBe(2);
    act(() => {
      if (!picker) return;
      picker.value = "ch0";
      picker.dispatchEvent(new Event("change", { bubbles: true }));
    });
    const shown = el.querySelector('[data-testid="chain"]');
    expect(shown?.getAttribute("data-chain-id")).toBe("ch0");
    expect(shown?.querySelector('[data-testid="chain-verdict"]')?.textContent).toContain("evidence against");
    expect(shown?.querySelector('[data-testid="chain-verdict"]')?.textContent).toContain("weighted sum");
  });
});

describe("the evidence readers", () => {
  const base = `c:readers:${CELL}:r1`;
  const run = ReadersRun.parse({
    reading_run_id: `r1:${CELL}`,
    run_id: "r1",
    arm: "d2",
    model: "claude-opus-5",
    verdict: "supports_closer_look",
    published: true,
    answer: {
      claims: [{ text: "The nearest conductor is 1.2 km away.", value_ids: [CONDUCTOR] }],
      unknown_criteria: ["alteration"],
      absent_criteria: [],
      next_observation: "a lake-sediment sample down-ice",
    },
    created_at: "2026-09-24T01:00:00+00:00",
    probability_id: `${base}:probability`,
    readings: [
      {
        family: "structure",
        assessment: "for",
        strength_id: `${base}:structure:strength`,
        published: true,
        summary: "A conductor runs along a lineament.",
        claims: [{ text: "1.2 km", value_ids: [CONDUCTOR] }],
      },
      {
        family: "dispersal",
        assessment: null,
        published: false,
        problems: ["cites 2.4% with no id"],
        summary: "",
        claims: [],
      },
    ],
  });

  it("shows the verdict and its probability, one row per reader, and a refused reading as refused", () => {
    registerValues(
      Object.fromEntries(
        [stat(`${base}:probability`, 0.72, "ratio3"), stat(`${base}:structure:strength`, 0.8, "ratio3")].map(
          (v) => [v.id, v],
        ),
      ),
      { notify: false },
    );
    const el = mount({ ...record, readers: [run] });
    const block = el.querySelector('[data-testid="readers"]');
    expect(block?.querySelector('[data-testid="readers-verdict"]')?.textContent).toContain(
      "supports a closer look",
    );
    expect(block?.querySelector(`[data-vid="${base}:probability"]`)?.textContent).toBe("0.720");
    const rows = Array.from(el.querySelectorAll('[data-testid="reading"]'));
    expect(rows.map((r) => r.getAttribute("data-family"))).toEqual(["structure", "dispersal"]);
    expect(rows[0]?.textContent).toContain("A conductor runs along a lineament.");
    expect(rows[0]?.querySelector(`[data-vid="${base}:structure:strength"]`)?.textContent).toBe("0.800");
    expect(rows[1]?.hasAttribute("data-published")).toBe(false);
    expect(rows[1]?.textContent).toContain("withheld");
    expect(rows[1]?.textContent).toContain("cites 2.4% with no id");
    expect(el.querySelector('[data-testid="readers-answer"]')?.textContent).toContain(
      "a lake-sediment sample down-ice",
    );
  });

  it("is absent for a cell with no readers run, and an older record parses with none", () => {
    const el = mount(record);
    expect(el.querySelector('[data-testid="readers"]')).toBeNull();
    const { readers: _drop, ...older } = record;
    expect(CellEvidence.parse(older).readers).toEqual([]);
  });
});
