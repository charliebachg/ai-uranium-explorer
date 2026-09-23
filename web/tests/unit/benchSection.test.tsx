import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it } from "vitest";
import { BenchBlock, type ValRegistry, type ValueId } from "@/data/contract";
import { registerValues } from "@/data/registry";
import { BenchSection, rowLabel } from "@/features/eval/PhaseSections";

/**
 * The analyst benchmark table with the staged loop's per-stage columns, rendered against a block shaped as
 * `ue prospect export` writes one. `<V>` throws on an unbacked id under vitest, so a render that completes is
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
      extra: {
        gate_rejection_rate: `${V1}:gate_rejection_rate`,
        cost_usd_per_cell: `${V1}:cost_usd_per_cell`,
      },
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
  it("prints every stage number of a staged arm through <V>, in its own table under the ranking", () => {
    const el = mount();
    const table = el.querySelector('[data-testid="bench-table"]');
    expect(table?.parentElement?.className).toContain("overflow-x-auto");
    const stages = el.querySelector('[data-testid="stage-table"]');
    for (const label of ["Chains", "Node gate", "Valid", "Caught", "Rounds", "Re-run"])
      expect(stages?.textContent).toContain(label);

    const staged = el.querySelector('[data-testid="stage-row"][data-row="v1-openrouter"]');
    expect(staged).not.toBeNull();
    for (const key of Object.keys(block.rows[1]?.stages ?? {})) {
      const cell = staged?.querySelector(`[data-stage="${key}"]`);
      expect(cell?.querySelector(`[data-vid="${V1}:stage:${key}"]`), key).not.toBeNull();
    }
    expect(staged?.querySelector('[data-stage="valid_rate"]')?.textContent).toBe("0.485");
    expect(staged?.querySelector('[data-stage="n_chains"]')?.textContent).toBe("130");

    // a single-call arm and a baseline have no stages, so they have no row there at all
    expect(el.querySelectorAll('[data-testid="stage-row"]').length).toBe(1);
    // with no ranking metric in the table, the rows rank by F1, arms and baselines together
    const order = Array.from(el.querySelectorAll('[data-testid="bench-row"]')).map((r) =>
      r.getAttribute("data-row"),
    );
    expect(order).toEqual(["v0", "random_expected", "v1-openrouter"]);
    // two models among the arms: the table carries a model column
    expect(table?.textContent).toContain("qwen/qwen3.8-flash · medium");
  });
});

describe("the ranking columns, the derived rows and the comparisons fixed before the runs", () => {
  const D1 = "c:bench:v2:d1";
  const VOTE = "c:bench:v2:d1-vote5";
  const EXT = "c:bench:v2:extended";
  const C = "c:bench:v2:contrast:d1~extended";
  const values: ValRegistry = Object.fromEntries(
    [
      stat(`${D1}:n`, 114, "int"),
      stat(`${D1}:pr_auc_rank`, 0.61, "ratio3"),
      stat(`${D1}:coverage`, 0.97, "ratio3"),
      stat(`${D1}:brier`, 0.24, "ratio3"),
      stat(`${VOTE}:n`, 114, "int"),
      stat(`${VOTE}:pr_auc_rank`, 0.66, "ratio3"),
      stat(`${EXT}:n`, 114, "int"),
      stat(`${EXT}:pr_auc_rank`, 0.52, "ratio3"),
      stat(`${C}:diff`, 0.09, "ratio3"),
      stat(`${C}:diff.lo`, -0.02, "ratio3"),
      stat(`${C}:diff.hi`, 0.2, "ratio3"),
      stat(`${C}:mcnemar_b`, 21, "int"),
      stat(`${C}:mcnemar_c`, 12, "int"),
      stat(`${C}:mcnemar_p`, 0.163, "ratio3"),
    ].map((v) => [v.id, v]),
  );
  const row = (name: string, kind: "arm" | "derived" | "baseline", pre: string, extra = {}) => ({
    name,
    kind,
    model: kind === "baseline" ? "extended" : "claude-opus-5",
    n: `${pre}:n`,
    n_pos: null,
    n_neg: null,
    run_id: null,
    mlflow_run_id: null,
    metrics: { pr_auc_rank: `${pre}:pr_auc_rank`, ...extra },
  });
  const v2 = BenchBlock.parse({
    version: "v2",
    manifest_sha256: null,
    computed_at: null,
    versions: [],
    rows: [
      row("extended", "baseline", EXT),
      row("d1-vote5", "derived", VOTE),
      row("d1", "arm", D1, { coverage: `${D1}:coverage`, brier: `${D1}:brier` }),
    ],
    contrasts: [
      {
        first: "d1",
        second: "extended",
        question: "the single-shot LLM against the fitted model",
        diff: `${C}:diff`,
        diff_ci: [`${C}:diff.lo`, `${C}:diff.hi`],
        mcnemar: { b: `${C}:mcnemar_b`, c: `${C}:mcnemar_c`, p: `${C}:mcnemar_p` },
      },
    ],
  });

  it("ranks every row by rank PR-AUC and prints each comparison through <V>", () => {
    registerValues(values, { notify: false });
    container = document.createElement("div");
    document.body.appendChild(container);
    root = createRoot(container);
    act(() => {
      root?.render(<BenchSection block={v2} />);
    });
    const el = container;
    const order = Array.from(el.querySelectorAll('[data-testid="bench-row"]')).map((r) =>
      r.getAttribute("data-row"),
    );
    expect(order).toEqual(["d1-vote5", "d1", "extended"]);
    expect(el.querySelector('[data-row="d1-vote5"]')?.textContent).toContain(
      "Single call · rich · vote of 5",
    );
    const d1 = el.querySelector('[data-testid="bench-row"][data-row="d1"]');
    expect(d1?.querySelector(`[data-vid="${D1}:pr_auc_rank"]`)?.textContent).toBe("0.610");
    expect(d1?.querySelector(`[data-vid="${D1}:coverage"]`)).not.toBeNull();
    const c = el.querySelector('[data-testid="bench-contrast"]');
    expect(c?.textContent).toContain("Single call · rich vs Extended model");
    expect(c?.getAttribute("title")).toBe("the single-shot LLM against the fitted model");
    for (const id of [`${C}:diff`, `${C}:mcnemar_b`, `${C}:mcnemar_c`, `${C}:mcnemar_p`])
      expect(c?.querySelector(`[data-vid="${id}"]`), id).not.toBeNull();
  });

  it("a table from before the comparisons existed parses with none", () => {
    expect(block.contrasts).toEqual([]);
  });
});

describe("the plain names", () => {
  it("names the arms, the rows derived from them and the baselines in words", () => {
    expect(rowLabel("v0")).toBe("Single call · basic");
    expect(rowLabel("d2")).toBe("Evidence readers · rich");
    expect(rowLabel("d2-fitted")).toBe("Evidence readers · rich · fitted weights");
    expect(rowLabel("d1~s3")).toBe("Single call · rich · sample 3");
    expect(rowLabel("extended")).toBe("Extended model");
    expect(rowLabel("v0-qwen38")).toBe("v0-qwen38");
  });
});
