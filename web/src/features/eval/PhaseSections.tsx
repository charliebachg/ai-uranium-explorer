import { useState } from "react";
import { Tip } from "@/components/ui/Tip";
import { V } from "@/components/values/V";
import type {
  BenchBlock,
  BenchContrast,
  BenchRow,
  BenchStage,
  BenchTable,
  HeadlineBlock,
  HeadlineRow,
  HindcastBlock,
  SearchBlock,
  SearchRow,
} from "@/data/contract";
import { resolveValue } from "@/data/registry";
import { Disclosure, Section } from "@/features/eval/EvalPage";
import { cn } from "@/lib/cn";

/**
 * The tracked evaluations behind the map's scores and the analyst benchmark. Nothing here is typed in: every
 * figure is a stored value from a run's own JSON, and the run id beside it names the MLflow run that holds the
 * parameters, the folds and the fitted model.
 */

const CORRECTION_LABEL = {
  naive: "as labelled",
  matched: "matched background",
  thinned: "thinned positives",
  "matched+thinned": "both",
} as const;

const FOLD_LABEL: Record<string, string> = {
  random: "random",
  spatial: "spatial",
  camp: "camp",
  spatial20: "20 km blocks",
  spatial50: "50 km blocks",
};

const ARM_LABEL: Record<string, string> = {
  histgb: "gradient boosting",
  random_forest: "random forest",
  logistic_spatial: "logistic + spatial terms",
  bagging_pu: "bagging PU",
  criteria_prior: "boosting + criteria prior",
  effort: "effort null",
  "learned+effort": "geology + effort",
};

const SEARCH_SHOWN = 10;

function RunId({ id }: { id: string | null | undefined }) {
  if (!id) return null;
  return (
    <span className="font-mono text-[10.5px] text-ink-3" title={id} data-ident>
      {id.slice(0, 12)}
    </span>
  );
}

function Naming({
  block,
}: {
  block: { run_id: string; snapshot?: string | null; store_sha256?: string | null };
}) {
  return (
    <p className="mt-2 text-[11px] text-ink-3">
      run <RunId id={block.run_id} />
      {block.store_sha256 ? (
        <>
          {" "}
          · store <RunId id={block.store_sha256} />
          {block.snapshot ? " · snapshot" : ""}
        </>
      ) : null}
    </p>
  );
}

function Interval({ ci }: { ci?: [string, string] }) {
  if (!ci) return null;
  return (
    <span className="ml-1 text-[10.5px] text-ink-3">
      <V id={ci[0]} />–<V id={ci[1]} />
    </span>
  );
}

function Th({ children, title, left }: { children: React.ReactNode; title?: string; left?: boolean }) {
  return (
    <th className={cn("py-1.5 font-normal", left ? "text-left" : "text-right")}>
      {title ? (
        <Tip text={title} className="border-line-strong border-b border-dotted">
          {children}
        </Tip>
      ) : (
        children
      )}
    </th>
  );
}

/** The re-test: effort against geology after the corrections the first result called for. */
export function HeadlineSection({ block }: { block?: HeadlineBlock }) {
  if (!block?.rows.length) return null;
  const pr = new Map<string, HeadlineRow>();
  for (const r of block.rows) if (r.metric === "pr_auc") pr.set(r.config, r);
  const lines: { positives: HeadlineRow["positives"]; correction: keyof typeof CORRECTION_LABEL }[] = [];
  for (const positives of ["all", "deposits"] as const)
    for (const correction of ["naive", "matched", "thinned", "matched+thinned"] as const)
      if (pr.has(`learned.${positives}.${correction}.spatial`)) lines.push({ positives, correction });
  const mt = new Map(block.minetrace.map((m) => [`${m.feature_set}.${m.metric}`, m.value_id]));
  return (
    <Section title="Geology vs effort" hint="PR-AUC under spatial folds, with bootstrap intervals.">
      <table className="w-full text-[12.5px]" data-testid="headline-table">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <Th left>Positives</Th>
            <Th
              left
              title="Matched background and thinned positives remove two ways the effort model is flattered."
            >
              Correction
            </Th>
            <Th>Geology</Th>
            <Th title="The null model: drilling and survey history only.">Effort</Th>
            <Th>Run</Th>
          </tr>
        </thead>
        <tbody>
          {lines.map(({ positives, correction }) => {
            const learned = pr.get(`learned.${positives}.${correction}.spatial`);
            const effort = pr.get(`effort.${positives}.${correction}.spatial`);
            return (
              <tr
                key={`${positives}.${correction}`}
                className="border-line border-t"
                data-testid="headline-row"
              >
                <td className="py-1.5 text-ink-2">
                  {positives === "all" ? "deposits + occurrences" : "deposits"}
                </td>
                <td className="py-1.5 text-ink-3">{CORRECTION_LABEL[correction]}</td>
                <td className="py-1.5 text-right">
                  {learned ? (
                    <>
                      <V id={learned.value_id} />
                      <Interval ci={learned.ci} />
                    </>
                  ) : (
                    <span className="text-ink-3">–</span>
                  )}
                </td>
                <td className="py-1.5 text-right">
                  {effort ? (
                    <>
                      <V id={effort.value_id} />
                      <Interval ci={effort.ci} />
                    </>
                  ) : (
                    <span className="text-ink-3">–</span>
                  )}
                </td>
                <td className="py-1.5 text-right">
                  <RunId id={learned?.run_id ?? effort?.run_id} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {mt.has("learned.roc_auc_mean") && mt.has("effort.roc_auc_mean") ? (
        <p className="mt-2 text-[11.5px] text-ink-3">
          MineTRACE protocol, ROC-AUC on deposits: geology{" "}
          <V id={mt.get("learned.roc_auc_mean") ?? ""} className="text-ink-2" />
          {mt.has("learned.roc_auc_sd") ? (
            <>
              {" "}
              ± <V id={mt.get("learned.roc_auc_sd") ?? ""} />
            </>
          ) : null}{" "}
          · effort <V id={mt.get("effort.roc_auc_mean") ?? ""} className="text-ink-2" />
          {mt.has("effort.roc_auc_sd") ? (
            <>
              {" "}
              ± <V id={mt.get("effort.roc_auc_sd") ?? ""} />
            </>
          ) : null}
        </p>
      ) : null}
      {block.verdict ? (
        <p className="mt-1 text-[11.5px] text-ink-2" data-testid="headline-verdict">
          Verdict: {block.verdict}
        </p>
      ) : null}
      <Naming block={block} />
    </Section>
  );
}

/** The candidates, the ablations and the block sizes, against the same null and the same folds. */
export function SearchSection({ block }: { block?: SearchBlock }) {
  const [all, setAll] = useState(false);
  if (!block?.rows.length) return null;
  const byArm = new Map<string, Partial<Record<string, SearchRow>>>();
  for (const r of block.rows) {
    const arm = byArm.get(r.arm) ?? {};
    arm[r.metric] = r;
    byArm.set(r.arm, arm);
  }
  const arms = [...byArm.entries()]
    .map(([arm, metrics]) => ({ arm, metrics, pr: numberOf(metrics.pr_auc?.value_id) }))
    .filter((a) => a.metrics.pr_auc)
    .sort((a, b) => (b.pr ?? -1) - (a.pr ?? -1));
  const shown = all ? arms : arms.slice(0, SEARCH_SHOWN);
  const label = (r: SearchRow) =>
    r.name.startsWith("ablation-")
      ? `boosting − ${r.name.slice("ablation-".length)}`
      : (ARM_LABEL[r.name] ?? r.name);
  return (
    <Section
      title="Model search"
      hint="A candidate is validated only if its interval clears the effort null's."
    >
      <table className="w-full text-[12.5px]" data-testid="search-table">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <Th left>Arm</Th>
            <Th left>Folds</Th>
            <Th>PR-AUC</Th>
            <Th title="Share of labelled cells in the top-scoring tenth of the grid.">
              <span data-chrome>capture@10%</span>
            </Th>
            <Th>Run</Th>
          </tr>
        </thead>
        <tbody>
          {shown.map(({ arm, metrics }) => {
            const pr = metrics.pr_auc;
            if (!pr) return null;
            return (
              <tr
                key={arm}
                className={cn("border-line border-t", pr.name === "effort" && "bg-white/[0.03]")}
                data-testid="search-row"
                data-arm={arm}
              >
                <td className="py-1.5 text-ink-2">
                  {label(pr)}
                  {pr.positives !== "all" ? <span className="text-ink-3"> · {pr.positives}</span> : null}
                </td>
                <td className="py-1.5 text-ink-3" data-chrome>
                  {FOLD_LABEL[pr.fold] ?? pr.fold}
                </td>
                <td className="py-1.5 text-right">
                  <V id={pr.value_id} />
                  <Interval ci={pr.ci} />
                </td>
                <td className="py-1.5 text-right">
                  {metrics.capture_top10 ? <V id={metrics.capture_top10.value_id} /> : null}
                </td>
                <td className="py-1.5 text-right">
                  <RunId id={pr.run_id} />
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {arms.length > SEARCH_SHOWN ? (
        <button
          type="button"
          onClick={() => setAll((v) => !v)}
          className="mt-1 text-[11.5px] text-ink-3 hover:text-ink-2"
          data-testid="search-more"
        >
          {all ? (
            "Show fewer"
          ) : (
            <>
              Show all <span data-instrument>{arms.length}</span>
            </>
          )}
        </button>
      ) : null}
      {block.decision ? <Decision block={block} /> : null}
      <Naming block={block} />
    </Section>
  );
}

/** The served-model decision, said with the values it was made from rather than the registry's free text. */
function Decision({ block }: { block: SearchBlock }) {
  const d = block.decision;
  if (!d) return null;
  const best = block.rows.find((r) => r.metric === "pr_auc" && r.run_id === d.run_id);
  const nul = block.rows.find(
    (r) => r.metric === "pr_auc" && r.name === "effort" && r.fold === "spatial" && r.positives === "all",
  );
  return (
    <div className="mt-2 text-[11.5px] text-ink-2" data-testid="search-decision" title={d.reason}>
      <p>
        Registry: <span data-ident>{d.model ?? "none"}</span> · <span data-ident>{d.stage ?? "none"}</span> ·{" "}
        {d.served ? "served" : "not served"}
        {best && nul ? (
          <>
            . Best geology <V id={best.value_id} />
            <Interval ci={best.ci} /> vs effort <V id={nul.value_id} />
            <Interval ci={nul.ci} />: {d.stage === "validated" ? "validated" : "not validated"}
          </>
        ) : null}
      </p>
      {d.card ? <ModelCard card={d.card} /> : null}
    </div>
  );
}

/** What the registered model was fitted on, from the tracker: the card a reader would ask for before trusting it. */
function ModelCard({ card }: { card: NonNullable<NonNullable<SearchBlock["decision"]>["card"]> }) {
  return (
    <Disclosure label="Model card" testId="model-card">
      <dl className="grid grid-cols-[auto_1fr] gap-x-4 gap-y-0.5 text-[11.5px]">
        <dt className="text-ink-3">model</dt>
        <dd data-ident>{card.name}</dd>
        <dt className="text-ink-3">features</dt>
        <dd data-ident>{card.feature_set}</dd>
        <dt className="text-ink-3">folds</dt>
        <dd data-chrome>
          {FOLD_LABEL[card.fold] ?? card.fold}
          {card.matched ? ", matched background" : ""}
          {card.thinned ? ", thinned positives" : ""}
        </dd>
        {card.scored && card.n_pos ? (
          <>
            <dt className="text-ink-3">cells</dt>
            <dd>
              <V id={card.scored} /> scored, <V id={card.n_pos} /> positive
            </dd>
          </>
        ) : null}
        {card.features.length ? (
          <>
            <dt className="text-ink-3">inputs</dt>
            <dd data-ident>{card.features.join(", ")}</dd>
          </>
        ) : null}
      </dl>
    </Disclosure>
  );
}

/** Prediction before the claim, with drilling and labels frozen at a cutoff. */
export function HindcastSection({ block }: { block?: HindcastBlock }) {
  if (!block?.rows.length) return null;
  const lines = new Map<
    string,
    {
      title: string;
      year: number | null;
      cutoff: number;
      cells: string[];
      by: Partial<Record<string, string>>;
    }
  >();
  for (const r of block.rows) {
    const key = `${r.cutoff}.${r.discovery}`;
    const line = lines.get(key) ?? { title: r.title, year: r.year, cutoff: r.cutoff, cells: r.cells, by: {} };
    line.by[r.model] = r.value_id;
    lines.set(key, line);
  }
  const summary = new Map(block.summary.map((s) => [`${s.model}.${s.key}`, s.value_id]));
  const models = ["learned", "effort", "criteria"] as const;
  return (
    <Section
      id="hindcast"
      title="Hindcast"
      hint="Share of the basin scoring at least as high as each later discovery. Lower is better."
    >
      <table className="w-full text-[12.5px]" data-testid="hindcast-table">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <Th left>Discovery</Th>
            <Th title="Labels and drilling frozen at this year.">Cutoff</Th>
            <Th>Geology</Th>
            <Th>Effort</Th>
            <Th>Criteria</Th>
          </tr>
        </thead>
        <tbody>
          {[...lines.values()].map((line) => (
            <tr
              key={`${line.cutoff}.${line.title}`}
              className="border-line border-t"
              data-testid="hindcast-row"
            >
              <td className="py-1.5 text-ink-2">
                <span data-ident>{line.title}</span>
                {line.year ? (
                  <span className="text-ink-3" data-chrome>
                    {" "}
                    ({line.year})
                  </span>
                ) : null}
              </td>
              <td className="py-1.5 text-right text-ink-3" data-chrome>
                {line.cutoff}
              </td>
              {models.map((m) => (
                <td key={m} className="py-1.5 text-right">
                  {line.by[m] ? <V id={line.by[m] ?? ""} /> : <span className="text-ink-3">–</span>}
                </td>
              ))}
            </tr>
          ))}
        </tbody>
        {models.some((m) => summary.has(`${m}.median_area_share`)) ? (
          <tfoot>
            <tr className="border-line border-t text-ink-2" data-testid="hindcast-summary">
              <td className="py-1.5" colSpan={2}>
                median
              </td>
              {models.map((m) => (
                <td key={m} className="py-1.5 text-right">
                  {summary.has(`${m}.median_area_share`) ? (
                    <V id={summary.get(`${m}.median_area_share`) ?? ""} />
                  ) : null}
                </td>
              ))}
            </tr>
          </tfoot>
        ) : null}
      </table>
      <p className="mt-2 text-[11.5px] text-ink-3">
        Not frozen: the compilations are as of today, and footprints and occurrences carry no date.
      </p>
      <Naming block={block} />
    </Section>
  );
}

// ---------------------------------------------------------------- the analyst benchmark

/** Plain names for the benchmark's rows. A name this map does not know prints as it is. */
const ROW_LABEL: Record<string, string> = {
  v0: "Single call · basic",
  d1: "Single call · rich",
  v1: "Staged loop · basic",
  d2: "Evidence readers · rich",
  extended: "Extended model",
  learned: "Learned model",
  criteria: "Criteria score",
  effort: "Effort null",
  random: "Random",
};

/** A row's plain name, including the rows derived from an arm (votes, fitted weights, samples). */
export function rowLabel(name: string): string {
  const vote = /^(.+)-vote(\d+)$/.exec(name);
  if (vote) return `${rowLabel(vote[1] ?? "")} · vote of ${vote[2] ?? ""}`;
  const fitted = /^(.+)-fitted$/.exec(name);
  if (fitted) return `${rowLabel(fitted[1] ?? "")} · fitted weights`;
  const sample = /^(.+)~s(\d+)$/.exec(name);
  if (sample) return `${rowLabel(sample[1] ?? "")} · sample ${sample[2] ?? ""}`;
  return ROW_LABEL[name] ?? name;
}

const KIND_LABEL: Record<BenchRow["kind"], string> = { arm: "LLM", derived: "derived", baseline: "baseline" };

/** A baseline hidden when it prints the same numbers as another row: `copy_learned` is `learned` whenever
 * every cell has a learned score. */
const SAME_AS: Record<string, string> = { copy_learned: "learned" };

/** The 2×2 the benchmark was run for: one call or several, on the basic pack or the rich one. */
const ABLATION: { label: string; basic: string; rich: string; design?: [string, string] }[] = [
  { label: "Single call", basic: "v0", rich: "d1" },
  { label: "Multi-agent", basic: "v1", rich: "d2", design: ["staged loop", "evidence readers"] },
];

/** The staged loop's per-chain columns, each with its heading and what it counts. */
const STAGE_COLUMNS: [BenchStage, string, string][] = [
  ["n_chains", "Chains", "Chains the staged loop ran."],
  ["gate_rejection_rate", "Node gate", "The gate's refusals over executor attempts."],
  ["valid_rate", "Valid", "Share of chains a verifier round validated."],
  ["verifier_catch_rate", "Caught", "Share the verifier refused at least once."],
  ["rounds_to_valid_mean", "Rounds", "Verifier rounds, over the chains that validated."],
  ["reexecuted_mean", "Re-run", "Nodes re-run on the verifier's feedback."],
  ["verifier_agreement_rate", "Verifier agrees", "How often the verifier matched the final verdict."],
  ["decider_agreement_rate", "Deciders agree", "How often the deciders matched the final verdict."],
];

/**
 * The analyst benchmark: one ranked table of every LLM arm, derived row and baseline on the same open cells,
 * the 2×2 ablation, and the comparisons fixed before the runs. The staged loop's per-chain numbers and the
 * earlier benchmark versions sit closed under the table.
 */
export function BenchSection({ block }: { block?: BenchBlock }) {
  if (!block?.rows.length) return null;
  const n = block.rows[0]?.n;
  const npos = block.rows.find((r) => r.n_pos)?.n_pos;
  return (
    <Section
      id="benchmark"
      title={
        <>
          Analyst benchmark <span data-ident>{block.version}</span>
        </>
      }
      aside={
        n ? (
          <span>
            <V id={n} /> scored cells
            {npos ? (
              <>
                {" "}
                · <V id={npos} /> positive
              </>
            ) : null}
          </span>
        ) : null
      }
      hint="Ranked by the model's own probability over every cell; a refusal counts as a coin flip."
    >
      <BenchTableView table={block} />
      <div className="mt-4 grid grid-cols-1 gap-4 md:grid-cols-[300px_1fr]">
        <Ablation table={block} />
        <BenchContrasts table={block} />
      </div>
      <StageDetail table={block} />
      {block.earlier.map((table) => (
        <Disclosure
          key={table.version}
          label={
            <>
              Earlier: benchmark <span data-ident>{table.version}</span>
            </>
          }
          testId="bench-earlier"
        >
          <BenchTableView table={table} />
          <BenchContrasts table={table} />
          <StageDetail table={table} />
        </Disclosure>
      ))}
      <BenchNaming table={block} />
    </Section>
  );
}

function rankOf(r: BenchRow): number | null {
  return numberOf(r.metrics.pr_auc_rank);
}

function signature(r: BenchRow): string {
  return JSON.stringify(
    (["pr_auc_rank", "f1", "brier", "coverage"] as const).map((m) => numberOf(r.metrics[m])),
  );
}

/** The rows a table prints, best first: by rank PR-AUC, then F1 for a row scored before ranking existed. */
function ranked(table: BenchTable): BenchRow[] {
  const ranks = table.rows.some((r) => rankOf(r) !== null);
  const byName = new Map(table.rows.map((r) => [r.name, r]));
  const rows = table.rows.filter((r) => {
    if (r.kind !== "baseline") return true;
    if (ranks && rankOf(r) === null) return false; // an analytic F1 with no ranking: nothing to rank it by
    const twin = SAME_AS[r.name] ? byName.get(SAME_AS[r.name] ?? "") : undefined;
    return !(twin && signature(twin) === signature(r));
  });
  const key = (r: BenchRow): [number, number] => {
    const rank = rankOf(r);
    return rank !== null ? [1, rank] : [0, numberOf(r.metrics.f1) ?? -1];
  };
  return rows.sort((a, b) => {
    const [ha, va] = key(a);
    const [hb, vb] = key(b);
    return hb - ha || vb - va;
  });
}

/** One version's ranked table. The model is named once above it when every LLM row ran on the same one. */
function BenchTableView({ table }: { table: BenchTable }) {
  const rows = ranked(table);
  const arms = rows.filter((r) => r.kind === "arm");
  const models = new Set(arms.map((r) => [r.model, r.effort].filter(Boolean).join(" · ")));
  const oneModel = models.size === 1 ? [...models][0] : null;
  return (
    <div className="mt-1" data-testid="bench-version" data-version={table.version}>
      {oneModel ? (
        <p className="mb-1 text-[11px] text-ink-3">
          LLM rows: <span data-ident>{oneModel}</span>
        </p>
      ) : null}
      <div className="overflow-x-auto">
        <table className="w-full text-[12.5px]" data-testid="bench-table">
          <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
            <tr>
              <Th left>Row</Th>
              <Th left>Type</Th>
              {oneModel ? null : <Th left>Model</Th>}
              <Th title="Every cell ranked by the stated probability, whatever the verdict; a refusal counts as a coin flip. Interval: bootstrap over cells.">
                Rank PR-AUC
              </Th>
              <Th title="F1 of the verdicts.">
                <span data-chrome>F1</span>
              </Th>
              <Th title="Share of cells with a usable probability.">Coverage</Th>
              <Th title="Squared error of the probability.">Brier</Th>
              <Th title="Share of cells answered as insufficient evidence.">Abstain</Th>
              <Th title="Share of answers the fabrication gate refused.">Gate</Th>
              <Th title="Cost per cell at list price.">$/cell</Th>
              <Th>Run</Th>
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <BenchLine key={`${r.kind}.${r.name}`} row={r} showModel={!oneModel} />
            ))}
          </tbody>
        </table>
      </div>
    </div>
  );
}

function BenchLine({ row, showModel }: { row: BenchRow; showModel: boolean }) {
  const llm = row.kind !== "baseline";
  const runId = row.mlflow_run_id ?? row.run_id;
  return (
    <tr
      className={cn("border-line border-t", !llm && "text-ink-3")}
      data-testid="bench-row"
      data-kind={row.kind}
      data-row={row.name}
      title={row.note}
    >
      <td className={cn("py-1.5", llm && "text-ink")}>
        <span data-ident>{rowLabel(row.name)}</span>
      </td>
      <td className="py-1.5">{KIND_LABEL[row.kind]}</td>
      {showModel ? (
        <td className="py-1.5">
          <span data-ident>{[row.model, row.effort].filter(Boolean).join(" · ") || "–"}</span>
        </td>
      ) : null}
      <td className="py-1.5 text-right">
        <Metric id={row.metrics.pr_auc_rank} ci={row.ci?.pr_auc_rank} />
      </td>
      <td className="py-1.5 text-right">
        <Metric id={row.metrics.f1} />
      </td>
      <td className="py-1.5 text-right">
        <Metric id={row.metrics.coverage} />
      </td>
      <td className="py-1.5 text-right">
        <Metric id={row.metrics.brier} />
      </td>
      <td className="py-1.5 text-right">
        <Metric id={row.metrics.abstain_rate} />
      </td>
      <td className="py-1.5 text-right">
        <Metric id={row.extra?.gate_rejection_rate} />
      </td>
      <td className="py-1.5 text-right">
        <Metric id={row.extra?.cost_usd_per_cell} />
      </td>
      <td className="py-1.5 text-right">
        {runId ? <RunId id={runId} /> : <span className="text-ink-3">–</span>}
      </td>
    </tr>
  );
}

/** The 2×2 ablation as a grid of rank PR-AUC, drawn when at least three of its four arms ran. */
function Ablation({ table }: { table: BenchTable }) {
  const byName = new Map(table.rows.map((r) => [r.name, r]));
  const present = ABLATION.flatMap((a) => [a.basic, a.rich]).filter((n) => byName.has(n)).length;
  if (present < 3) return null;
  const cell = (name: string, design?: string) => {
    const r = byName.get(name);
    return (
      <td className="rounded-lg bg-black/25 px-3 py-2 text-right" data-testid="ablation-cell" data-row={name}>
        <div className="text-[18px] text-ink leading-none">
          <Metric id={r?.metrics.pr_auc_rank} />
        </div>
        {design ? <div className="mt-1 text-[10.5px] text-ink-3">{design}</div> : null}
      </td>
    );
  };
  return (
    <div data-testid="ablation">
      <h3 className="mb-1.5 text-[12px] text-ink-2">LLM ablation · rank PR-AUC</h3>
      <table className="w-full border-separate border-spacing-1 text-[12px]">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th />
            <th className="text-right font-normal">
              <Tip
                text="The benchmark's first evidence pack."
                className="border-line-strong border-b border-dotted"
              >
                Basic
              </Tip>
            </th>
            <th className="text-right font-normal">
              <Tip
                text="Adds evidence tools, the region and the extended features."
                className="border-line-strong border-b border-dotted"
              >
                Rich
              </Tip>
            </th>
          </tr>
        </thead>
        <tbody>
          {ABLATION.map((a) => (
            <tr key={a.label}>
              <td className="pr-2 text-ink-3">{a.label}</td>
              {cell(a.basic, a.design?.[0])}
              {cell(a.rich, a.design?.[1])}
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

/** The comparisons fixed before the runs: a paired difference in rank PR-AUC, and McNemar on verdicts. */
function BenchContrasts({ table }: { table: BenchTable }) {
  if (!table.contrasts.length) return null;
  return (
    <div className="overflow-x-auto">
      <h3 className="mb-1.5 text-[12px] text-ink-2">Paired comparisons</h3>
      <table className="w-full text-[12px]" data-testid="bench-contrasts">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <Th left>A vs B</Th>
            <Th title="Rank PR-AUC of A minus B on the same cells, with a paired bootstrap interval.">Δ</Th>
            <Th title="Cells only A got right, and only B.">Only A · B</Th>
            <Th title="McNemar's exact test on which cells each got right.">p</Th>
          </tr>
        </thead>
        <tbody>
          {table.contrasts.map((c) => (
            <ContrastLine key={`${c.first}~${c.second}`} c={c} />
          ))}
        </tbody>
      </table>
    </div>
  );
}

function ContrastLine({ c }: { c: BenchContrast }) {
  const lo = numberOf(c.diff_ci?.[0]);
  const hi = numberOf(c.diff_ci?.[1]);
  const tone = lo !== null && lo > 0 ? "text-st-pass" : hi !== null && hi < 0 ? "text-st-miss" : "text-ink-2";
  return (
    <tr className="border-line border-t" data-testid="bench-contrast" title={c.question}>
      <td className="py-1.5">
        <span data-ident className="text-ink-2">
          {rowLabel(c.first)}
        </span>
        <span className="text-ink-3"> vs </span>
        <span data-ident className="text-ink-2">
          {rowLabel(c.second)}
        </span>
      </td>
      <td className="py-1.5 text-right">
        <V id={c.diff} className={tone} />
        <Interval ci={c.diff_ci} />
      </td>
      <td className="py-1.5 text-right text-ink-3">
        {c.mcnemar ? (
          <>
            <V id={c.mcnemar.b} /> · <V id={c.mcnemar.c} />
          </>
        ) : (
          "–"
        )}
      </td>
      <td className="py-1.5 text-right">
        <Metric id={c.mcnemar?.p} />
      </td>
    </tr>
  );
}

/** The staged loop's per-chain numbers, for the rows that have stages; closed until asked for. */
function StageDetail({ table }: { table: BenchTable }) {
  const staged = table.rows.filter((r) => r.stages && Object.keys(r.stages).length);
  if (!staged.length) return null;
  return (
    <Disclosure label="Staged loop, per chain" testId="bench-stages">
      <div className="overflow-x-auto">
        <table className="w-full text-[12px]" data-testid="stage-table">
          <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
            <tr>
              <Th left>Row</Th>
              {STAGE_COLUMNS.map(([key, label, title]) => (
                <Th key={key} title={title}>
                  {label}
                </Th>
              ))}
            </tr>
          </thead>
          <tbody>
            {staged.map((r) => (
              <tr key={r.name} className="border-line border-t" data-testid="stage-row" data-row={r.name}>
                <td className="py-1.5 text-ink-2">
                  <span data-ident>{rowLabel(r.name)}</span>
                </td>
                {STAGE_COLUMNS.map(([key]) => (
                  <td key={key} className="py-1.5 text-right" data-stage={key}>
                    <Metric id={r.stages?.[key]} />
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Disclosure>
  );
}

/** A stored value with its interval, or a dash where the table has none: never a typed-in figure. */
function Metric({ id, ci }: { id?: string; ci?: [string, string] }) {
  if (!id) return <span className="text-ink-3">–</span>;
  return (
    <>
      <V id={id} />
      <Interval ci={ci} />
    </>
  );
}

/** What the table was scored against: the benchmark version, its manifest, and when it was computed. */
function BenchNaming({ table }: { table: BenchTable }) {
  return (
    <p className="mt-3 text-[11px] text-ink-3">
      benchmark <span data-ident>{table.version}</span>
      {table.manifest_sha256 ? (
        <>
          {" "}
          · manifest <RunId id={table.manifest_sha256} />
        </>
      ) : null}
      {table.computed_at ? (
        <>
          {" "}
          · <span data-chrome>{table.computed_at.slice(0, 16).replace("T", " ")}</span>
        </>
      ) : null}
    </p>
  );
}

function numberOf(id: string | undefined): number | null {
  const v = id ? resolveValue(id)?.value : null;
  return typeof v === "number" ? v : null;
}
