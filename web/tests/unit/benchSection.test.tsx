import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { BenchBlock, type ValRegistry, type ValueId } from "@/data/contract";
import { registerValues } from "@/data/registry";
import { BenchSection } from "@/features/eval/PhaseSections";

/**
 * The analyst benchmark table with the staged loop's per-stage columns, rendered against a block shaped as
 * `lr prospect export` writes one. `<V>` throws on an unbacked id under vitest, so a render that completes is
 * itself the assertion that every stage number the table prints resolves to a registered value.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

function stat(id: string, value: number, fmt: "ratio3" | "int" | "m1" | "m2") {
  return { id: id as ValueId, kind: "stat" as const, as_printed: null, value, unit_as_printed: null, fmt };
}

const V1 = "c:bench:v1:v1-openrouter";
const V0 = "c:bench:v1:v0";
const BASE = "c:bench:v1:random_expected";

const VALUES: ValRegistry = Object.fromEntries(
  [
    stat(`${V1}:n`, 114, "int"),
    stat(`${V1}:f1`, 0.286, "ratio3"),
    stat(`${V1}:pr_auc`, 0.556, "ratio3"),
    stat(`${V1}:abstain_rate`, 0.816, "ratio3"),
    stat(`${V1}:gate_rejection_rate`, 0.592, "ratio3"),
    stat(`${V1}:cost_usd_per_cell`, 0.05, "m2"),
    stat(`${V1}:stage:n_chains`, 130, "int"),
    stat(`${V1}:stage:gate_rejection_rate`, 0.0908, "ratio3"),
    stat(`${V1}:stage:valid_rate`, 0.4846, "ratio3"),
    stat(`${V1}:stage:verifier_catch_rate`, 0.7308, "ratio3"),
    stat(`${V1}:stage:rounds_to_valid_mean`, 1.6032, "m2"),
    stat(`${V1}:stage:reexecuted_mean`, 3.6769, "m2"),
    stat(`${V1}:stage:verifier_agreement_rate`, 0.6692, "ratio3"),
    stat(`${V1}:stage:decider_agreement_rate`, 0.6154, "ratio3"),
    stat(`${V0}:n`, 114, "int"),
    stat(`${V0}:f1`, 0.531, "ratio3"),
    stat(`${V0}:pr_auc`, 0.523, "ratio3"),
    stat(`${V0}:abstain_rate`, 0.24, "ratio3"),
    stat(`${BASE}:n`, 114, "int"),
    stat(`${BASE}:f1`, 0.47, "ratio3"),
  ].map((v) => [v.id, v]),
);

const block = BenchBlock.parse({
  version: "v1",
  manifest_sha256: "b".repeat(64),
  computed_at: "2026-09-21T10:00:00+00:00",
  versions: [],
  rows: [
    {
      name: "v0",
      kind: "arm",
      model: "claude-opus-5",
      effort: "medium",
      n: `${V0}:n`,
      n_pos: null,
      n_neg: null,
      run_id: "20260920T061420Z-bench",
      mlflow_run_id: null,
      metrics: { f1: `${V0}:f1`, pr_auc: `${V0}:pr_auc`, abstain_rate: `${V0}:abstain_rate` },
    },
    {
      name: "v1-openrouter",
      kind: "arm",
      model: "qwen/qwen3.8-flash",
      effort: "medium",
      n: `${V1}:n`,
      n_pos: null,
      n_neg: null,
      run_id: "20260920T181356Z-bench",
      mlflow_run_id: "4cbd562b",
      metrics: { f1: `${V1}:f1`, pr_auc: `${V1}:pr_auc`, abstain_rate: `${V1}:abstain_rate` },
      extra: { gate_rejection_rate: `${V1}:gate_rejection_rate`, cost_usd_per_cell: `${V1}:cost_usd_per_cell` },
      stages: {
        n_chains: `${V1}:stage:n_chains`,
        gate_rejection_rate: `${V1}:stage:gate_rejection_rate`,
        valid_rate: `${V1}:stage:valid_rate`,
        verifier_catch_rate: `${V1}:stage:verifier_catch_rate`,
        rounds_to_valid_mean: `${V1}:stage:rounds_to_valid_mean`,
        reexecuted_mean: `${V1}:stage:reexecuted_mean`,
        verifier_agreement_rate: `${V1}:stage:verifier_agreement_rate`,
        decider_agreement_rate: `${V1}:stage:decider_agreement_rate`,
      },
    },
    {
      name: "random_expected",
      kind: "baseline",
      model: "random",
      n: `${BASE}:n`,
      n_pos: null,
      n_neg: null,
      run_id: null,
      mlflow_run_id: null,
      note: "analytic expectation; no interval",
      metrics: { f1: `${BASE}:f1` },
    },
  ],
});

let root: Root | null = null;
let container: HTMLElement | null = null;

function mount(): HTMLElement {
  registerValues(VALUES, { notify: false });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  act(() => {
    root?.render(<BenchSection block={block} />);
  });
  return container;
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = null;
  container = null;
});

describe("the analyst benchmark table with stage columns", () => {
  it("prints every stage number of a staged arm through <V>, and a dash where an arm has no stage", () => {
    const el = mount();
    const table = el.querySelector('[data-testid="bench-table"]');
    expect(table?.parentElement?.className).toContain("overflow-x-auto");
    expect(table?.textContent).toContain("the staged loop, per chain");
    for (const label of ["Chains", "Node gate", "Valid", "Caught", "Rounds to valid", "Re-executed"])
      expect(table?.textContent).toContain(label);

    const staged = el.querySelector('[data-testid="bench-row"][data-row="v1-openrouter"]');
    expect(staged).not.toBeNull();
    for (const key of Object.keys(block.rows[1]?.stages ?? {})) {
      const cell = staged?.querySelector(`[data-stage="${key}"]`);
      expect(cell?.querySelector(`[data-vid="${V1}:stage:${key}"]`), key).not.toBeNull();
    }
    // the digits the stage cells print all sit under a value id: nothing is typed in
    for (const cell of Array.from(staged?.querySelectorAll("[data-stage]") ?? []))
      for (const digit of cell.textContent?.match(/\d/g) ?? []) expect(digit).toBeTruthy();
    expect(staged?.querySelector('[data-stage="valid_rate"]')?.textContent).toBe("0.485");
    expect(staged?.querySelector('[data-stage="n_chains"]')?.textContent).toBe("130");

    // the single-call arm and the baseline have no stages: a dash, never a number
    for (const name of ["v0", "random_expected"]) {
      const row = el.querySelector(`[data-testid="bench-row"][data-row="${name}"]`);
      const cells = Array.from(row?.querySelectorAll("[data-stage]") ?? []);
      expect(cells.length).toBe(8);
      for (const cell of cells) {
        expect(cell.textContent).toBe("–");
        expect(cell.querySelector("[data-vid]")).toBeNull();
      }
    }
    // arms first, baselines after, as before
    const kinds = Array.from(el.querySelectorAll('[data-testid="bench-row"]')).map((r) => r.getAttribute("data-kind"));
    expect(kinds).toEqual(["arm", "arm", "baseline"]);
  });
});
