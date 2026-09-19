import { V } from "@/components/values/V";
import type { HeadlineBlock, HeadlineRow, HindcastBlock, SearchBlock, SearchRow } from "@/data/contract";
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

function numberOf(id: string | undefined): number | null {
  const v = id ? resolveValue(id)?.value : null;
  return typeof v === "number" ? v : null;
}
