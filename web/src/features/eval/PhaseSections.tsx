import { V } from "@/components/values/V";
import type {
  BenchBlock,
  BenchRow,
  BenchStage,
  HeadlineBlock,
  HeadlineRow,
  HindcastBlock,
  SearchBlock,
  SearchRow,
} from "@/data/contract";
import { resolveValue } from "@/data/registry";
import { Section } from "@/features/eval/EvalPage";
import { cn } from "@/lib/cn";

/**
 * The three tracked evaluations behind the map's scores, each row naming the tracker run it came from. Nothing
 * here is typed in: every figure is a stored value from the run's own JSON, and the run id beside it is the
 * MLflow run that holds the parameters, the fold assignments and the fitted model.
 */

const CORRECTION_LABEL = {
  naive: "as labelled",
  matched: "matched background",
  thinned: "thinned positives",
  "matched+thinned": "both corrections",
} as const;

const FOLD_LABEL: Record<string, string> = {
  random: "random folds",
  spatial: "spatial folds",
  camp: "camp folds",
  spatial20: "20 km blocks",
  spatial50: "50 km blocks",
};

const ARM_LABEL: Record<string, string> = {
  histgb: "gradient boosting",
  random_forest: "random forest",
  logistic_spatial: "logistic with spatial terms",
  bagging_pu: "bagging PU",
  criteria_prior: "boosting + criteria score as a prior",
  effort: "effort (null model)",
  "learned+effort": "geology + effort",
};

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
          {block.snapshot ? " (a named snapshot)" : " (no snapshot names it)"}
        </>
      ) : null}
    </p>
  );
}

function Interval({ ci }: { ci?: [string, string] }) {
  if (!ci) return null;
  return (
    <span className="ml-1 text-[10.5px] text-ink-3">
      <V id={ci[0]} /> to <V id={ci[1]} />
    </span>
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
    <Section
      title="The re-test: does geology beat where people already drilled?"
      hint="PR-AUC under spatial folds, out of fold, with bootstrap intervals. Effort is the null model that knows only the drilling history; the corrections remove the two ways the first comparison could have flattered it."
    >
      <table className="w-full text-[12.5px]" data-testid="headline-table">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="py-1.5 text-left font-normal">Positives</th>
            <th className="py-1.5 text-left font-normal">Correction</th>
            <th className="py-1.5 text-right font-normal">Geology</th>
            <th className="py-1.5 text-right font-normal">Effort</th>
            <th className="py-1.5 text-right font-normal">run</th>
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
                  {positives === "all" ? "deposits + occurrences" : "deposits only"}
                </td>
                <td className="py-1.5 text-ink-2">{CORRECTION_LABEL[correction]}</td>
                <td className="py-1.5 text-right">
                  {learned ? (
                    <>
                      <V id={learned.value_id} />
                      <Interval ci={learned.ci} />
                    </>
                  ) : (
                    <span className="text-ink-3">not measurable</span>
                  )}
                </td>
                <td className="py-1.5 text-right">
                  {effort ? (
                    <>
                      <V id={effort.value_id} />
                      <Interval ci={effort.ci} />
                    </>
                  ) : (
                    <span className="text-ink-3">not measurable</span>
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
          MineTRACE's own protocol (ROC-AUC on deposits, a fixed draw of positives against background cells,
          repeated): geology <V id={mt.get("learned.roc_auc_mean") ?? ""} className="text-ink-2" />
          {mt.has("learned.roc_auc_sd") ? (
            <>
              {" "}
              ± <V id={mt.get("learned.roc_auc_sd") ?? ""} className="text-ink-2" />
            </>
          ) : null}
          , effort <V id={mt.get("effort.roc_auc_mean") ?? ""} className="text-ink-2" />
          {mt.has("effort.roc_auc_sd") ? (
            <>
              {" "}
              ± <V id={mt.get("effort.roc_auc_sd") ?? ""} className="text-ink-2" />
            </>
          ) : null}
          .
        </p>
      ) : null}
      {block.verdict ? (
        <p className="mt-2 max-w-[86ch] text-[11.5px] text-ink-2" data-testid="headline-verdict">
          Verdict: {block.verdict}
        </p>
      ) : null}
      <Naming block={block} />
    </Section>
  );
}

/** The candidates, the ablations and the block sizes, against the same null and the same folds. */
export function SearchSection({ block }: { block?: SearchBlock }) {
  if (!block?.rows.length) return null;
  const byArm = new Map<string, Partial<Record<string, SearchRow>>>();
  for (const r of block.rows) {
    const arm = byArm.get(r.arm) ?? {};
    arm[r.metric] = r;
    byArm.set(r.arm, arm);
  }
  const arms = [...byArm.entries()]
    .map(([arm, metrics]) => ({ arm, metrics, pr: numberOf(metrics.pr_auc?.value_id) }))
    .sort((a, b) => (b.pr ?? -1) - (a.pr ?? -1));
  const label = (r: SearchRow) =>
    r.name.startsWith("ablation-")
      ? `boosting without ${r.name.slice("ablation-".length)}`
      : (ARM_LABEL[r.name] ?? r.name);
  return (
    <Section
      title="The model search"
      hint="Every arm is one tracked run, scored out of fold on the same cells as the null model. A candidate is validated only if its interval lies wholly above the null's; the decision is applied by the code, not read off the table."
    >
      <table className="w-full text-[12.5px]" data-testid="search-table">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="py-1.5 text-left font-normal">Arm</th>
            <th className="py-1.5 text-left font-normal">Held out by</th>
            <th className="py-1.5 text-right font-normal">PR-AUC</th>
            <th className="py-1.5 text-right font-normal">
              <span data-chrome>capture@10%</span>
            </th>
            <th className="py-1.5 text-right font-normal">run</th>
          </tr>
        </thead>
        <tbody>
          {arms.map(({ arm, metrics }) => {
            const pr = metrics.pr_auc;
            if (!pr) return null;
            const isNull = pr.name === "effort";
            return (
              <tr
                key={arm}
                className={cn("border-line border-t", isNull && "bg-white/[0.03]")}
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
    <p className="mt-2 max-w-[86ch] text-[11.5px] text-ink-2" data-testid="search-decision" title={d.reason}>
      Registry: <span data-ident>{d.model ?? "none"}</span> at stage{" "}
      <span data-ident>{d.stage ?? "none"}</span>, served <span data-ident>{d.served ? "yes" : "no"}</span>.
      {best && nul ? (
        <>
          {" "}
          The best geology-only candidate reached <V id={best.value_id} />
          <Interval ci={best.ci} /> against the effort null's <V id={nul.value_id} />
          <Interval ci={nul.ci} /> under spatial folds
          {d.stage === "validated" ? ": validated." : ": not validated, so nothing is served."}
        </>
      ) : null}
      {d.card ? <ModelCard card={d.card} /> : null}
    </p>
  );
}

/** What the registered model was fitted on, from the tracker: the card a reader would ask for before trusting it. */
function ModelCard({ card }: { card: NonNullable<NonNullable<SearchBlock["decision"]>["card"]> }) {
  return (
    <span className="mt-1 block text-[11px] text-ink-3" data-testid="model-card">
      Model card: <span data-ident>{card.name}</span> on the <span data-ident>{card.feature_set}</span>{" "}
      feature set, held out by <span data-chrome>{FOLD_LABEL[card.fold] ?? card.fold}</span>
      {card.matched ? ", matched background" : ""}
      {card.thinned ? ", thinned positives" : ""}
      {card.scored && card.n_pos ? (
        <>
          ; <V id={card.scored} /> cells scored with <V id={card.n_pos} /> positives
        </>
      ) : null}
      {card.features.length ? (
        <>
          ; features:{" "}
          {card.features.map((f, i) => (
            <span key={f}>
              {i ? ", " : ""}
              <span data-ident>{f}</span>
            </span>
          ))}
        </>
      ) : null}
      .
    </span>
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
      title="The dated hindcast"
      hint="Labels and drilling frozen at the cutoff year; the grid scored; each later discovery reported as the share of basin area that scored at least as well. Smaller is better; a coin toss would put a discovery halfway down."
    >
      <table className="w-full text-[12.5px]" data-testid="hindcast-table">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="py-1.5 text-left font-normal">Discovery</th>
            <th className="py-1.5 text-right font-normal">Cutoff</th>
            <th className="py-1.5 text-right font-normal">Geology</th>
            <th className="py-1.5 text-right font-normal">Effort</th>
            <th className="py-1.5 text-right font-normal">Criteria</th>
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
                median over the rows above
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
      <p className="mt-2 max-w-[86ch] text-[11.5px] text-ink-3">
        What cannot be frozen: the conductor, fault and geochemistry compilations are drawn as they stand
        today, survey footprints carry no year, and occurrences carry no date. A discovery next to one already
        known before the cutoff is ranked by proximity as much as by prediction.
      </p>
      <Naming block={block} />
    </Section>
  );
}

/** The staged loop's per-stage columns in the order the table prints them, each with its heading. */
const STAGE_COLUMNS: [BenchStage, string][] = [
  ["n_chains", "Chains"],
  ["gate_rejection_rate", "Node gate"],
  ["valid_rate", "Valid"],
  ["verifier_catch_rate", "Caught"],
  ["rounds_to_valid_mean", "Rounds to valid"],
  ["reexecuted_mean", "Re-executed"],
  ["verifier_agreement_rate", "Verifier agrees"],
  ["decider_agreement_rate", "Deciders agree"],
];

/**
 * The analyst benchmark: every arm and every baseline scored on the same open cells, arms first, each group
 * ranked by F1. A baseline row is muted: it is arithmetic on the fitted scores, not a run, and names none.
 * The second group of columns is the staged loop's per-stage metrics, per chain; a single-call arm and a
 * baseline have no stages and print a dash there. The table is wider than a phone, so it scrolls inside its
 * own container rather than the page.
 */
export function BenchSection({ block }: { block?: BenchBlock }) {
  if (!block?.rows.length) return null;
  const byF1 = (a: BenchRow, b: BenchRow) => (numberOf(b.metrics.f1) ?? -1) - (numberOf(a.metrics.f1) ?? -1);
  const rows = [
    ...block.rows.filter((r) => r.kind === "arm").sort(byF1),
    ...block.rows.filter((r) => r.kind === "baseline").sort(byF1),
  ];
  return (
    <Section
      title="The analyst benchmark"
      hint="One row is one arm or one baseline on the same open cells of the frozen benchmark. Probe cells are not scored; an abstention is never a positive, so it counts against recall, and an answer the gate refused is an abstention too. Intervals are bootstraps over cells. The staged loop's columns are per chain, from the run's own rows: the node gate's refusals over executor attempts, the share of chains a verifier round validated, the share the verifier refused at least once, rounds over the chains that validated, nodes re-executed on the verifier's feedback, and how often the verifier's own label and the weighted-sum decider agreed with the final verdict. A single-call arm has no stages and shows none; the shallow-path share and the refusals per leakage rule stay in the run summary, being the same in every arm so far."
    >
      <div className="overflow-x-auto">
        <table className="w-full text-[12.5px]" data-testid="bench-table">
          <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
            <tr>
              <th colSpan={10} className="py-1 text-left font-normal">
                against the labels
              </th>
              <th
                colSpan={STAGE_COLUMNS.length}
                className="border-line border-l py-1 pl-3 text-left font-normal"
              >
                the staged loop, per chain
              </th>
            </tr>
            <tr>
              <th className="py-1.5 text-left font-normal">Row</th>
              <th className="py-1.5 text-left font-normal">Kind</th>
              <th className="py-1.5 text-left font-normal">Model</th>
              <th className="py-1.5 text-right font-normal">n</th>
              <th className="py-1.5 text-right font-normal">
                <span data-chrome>F1</span>
              </th>
              <th className="py-1.5 text-right font-normal">PR-AUC</th>
              <th className="py-1.5 text-right font-normal">Abstain</th>
              <th className="py-1.5 text-right font-normal">Gate rejected</th>
              <th className="py-1.5 text-right font-normal">$ per cell</th>
              <th className="py-1.5 text-right font-normal">run</th>
              {STAGE_COLUMNS.map(([key, label], i) => (
                <th
                  key={key}
                  className={cn("py-1.5 pl-3 text-right font-normal", i === 0 && "border-line border-l")}
                >
                  {label}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {rows.map((r) => (
              <BenchLine key={`${r.kind}.${r.name}`} row={r} />
            ))}
          </tbody>
        </table>
      </div>
      <BenchNaming block={block} />
    </Section>
  );
}

function BenchLine({ row }: { row: BenchRow }) {
  const baseline = row.kind === "baseline";
  const runId = row.mlflow_run_id ?? row.run_id;
  return (
    <tr
      className={cn("border-line border-t", baseline && "text-ink-3")}
      data-testid="bench-row"
      data-kind={row.kind}
      data-row={row.name}
      title={row.note}
    >
      <td className={cn("py-1.5", !baseline && "text-ink-2")}>
        <span data-ident>{row.name}</span>
      </td>
      <td className="py-1.5">{row.kind}</td>
      <td className="py-1.5">
        <span data-ident>{row.model ?? "–"}</span>
        {row.effort ? (
          <span className="text-ink-3" data-chrome>
            {" "}
            · {row.effort}
          </span>
        ) : null}
      </td>
      <td className="py-1.5 text-right">
        <V id={row.n} />
      </td>
      <td className="py-1.5 text-right">
        <Metric id={row.metrics.f1} ci={row.ci?.f1} />
      </td>
      <td className="py-1.5 text-right">
        <Metric id={row.metrics.pr_auc} ci={row.ci?.pr_auc} />
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
      {STAGE_COLUMNS.map(([key], i) => (
        <td
          key={key}
          className={cn("py-1.5 pl-3 text-right", i === 0 && "border-line border-l")}
          data-stage={key}
        >
          <Metric id={row.stages?.[key]} />
        </td>
      ))}
    </tr>
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
function BenchNaming({ block }: { block: BenchBlock }) {
  return (
    <p className="mt-2 text-[11px] text-ink-3">
      benchmark <span data-ident>{block.version}</span>
      {block.manifest_sha256 ? (
        <>
          {" "}
          · manifest <RunId id={block.manifest_sha256} />
        </>
      ) : null}
      {block.computed_at ? (
        <>
          {" "}
          · computed <span data-chrome>{block.computed_at.slice(0, 16).replace("T", " ")}</span>
        </>
      ) : null}
      {block.versions.length ? (
        <>
          {" "}
          · earlier tables not shown:{" "}
          {block.versions.map((v, i) => (
            <span key={v}>
              {i ? ", " : ""}
              <span data-ident>{v}</span>
            </span>
          ))}
        </>
      ) : null}
    </p>
  );
}

function numberOf(id: string | undefined): number | null {
  const v = id ? resolveValue(id)?.value : null;
  return typeof v === "number" ? v : null;
}
