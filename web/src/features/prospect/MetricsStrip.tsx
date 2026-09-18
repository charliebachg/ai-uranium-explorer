import { V } from "@/components/values/V";
import type { MetricRow, Readiness } from "@/data/contract";
import { resolveValue } from "@/data/registry";
import { cn } from "@/lib/cn";

/**
 * What the three scores are worth, from the readiness export's own metric table. The row that matters is the
 * spatial fold: split at random and a model can learn the neighbourhood of a known deposit and score well on
 * the rest of it, so the honest comparison is between the geological model and the exploration-effort null
 * model under folds that keep a camp together.
 */

const MODEL_LABEL: Record<MetricRow["model"], string> = {
  criteria: "Criteria",
  learned: "Learned",
  effort: "Effort (null model)",
};

const FOLD_LABEL: Record<MetricRow["fold"], string> = {
  none: "no fold (fitted and scored on the same cells)",
  random: "random folds",
  spatial: "spatial folds",
  camp: "camp folds",
};

const MODEL_ORDER: MetricRow["model"][] = ["criteria", "learned", "effort"];
const FOLD_ORDER: MetricRow["fold"][] = ["none", "random", "spatial", "camp"];

type Key = `${MetricRow["model"]}.${MetricRow["fold"]}.${MetricRow["metric"]}`;

function index(rows: MetricRow[]): Map<string, string> {
  const out = new Map<string, string>();
  for (const r of rows) out.set(`${r.model}.${r.fold}.${r.metric}`, r.value_id);
  return out;
}

function numberOf(id: string | undefined): number | null {
  const v = id ? resolveValue(id)?.value : null;
  return typeof v === "number" ? v : null;
}

export function MetricsStrip({ data }: { data: Readiness }) {
  const rows = data.metrics?.rows ?? [];
  if (!rows.length) {
    return (
      <p className="text-[11.5px] text-ink-3">
        The readiness export carries no metric table, so nothing here says what the scores are worth.
      </p>
    );
  }
  const by = index(rows);
  const pairs: { model: MetricRow["model"]; fold: MetricRow["fold"] }[] = [];
  for (const model of MODEL_ORDER) {
    for (const fold of FOLD_ORDER) {
      if (by.has(`${model}.${fold}.pr_auc` satisfies Key)) pairs.push({ model, fold });
    }
  }

  return (
    <div>
      <table className="w-full text-[12.5px]" data-testid="metrics-table">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="py-1.5 text-left font-normal">Model</th>
            <th className="py-1.5 text-left font-normal">Held out by</th>
            <th className="w-[92px] py-1.5 text-right font-normal">PR-AUC</th>
            <th className="w-[112px] py-1.5 text-right font-normal">
              <span data-chrome>capture@10%</span>
            </th>
            <th className="w-[92px] py-1.5 text-right font-normal">Base rate</th>
          </tr>
        </thead>
        <tbody>
          {pairs.map(({ model, fold }) => (
            <tr
              key={`${model}.${fold}`}
              className="border-line border-t"
              data-testid="metric-row"
              data-model={model}
              data-fold={fold}
            >
              <td className={cn("py-1.5", model === "effort" ? "text-st-flag" : "text-ink-2")}>
                {MODEL_LABEL[model]}
              </td>
              <td className="py-1.5 text-ink-3">{FOLD_LABEL[fold]}</td>
              <td className="py-1.5 text-right">
                <V id={by.get(`${model}.${fold}.pr_auc`) ?? null} />
              </td>
              <td className="py-1.5 text-right">
                <V id={by.get(`${model}.${fold}.capture_top10`) ?? null} />
              </td>
              <td className="py-1.5 text-right text-ink-3">
                <V id={by.get(`${model}.${fold}.base_rate`) ?? null} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
      <SpatialFoldLine by={by} />
      <p className="mt-1.5 max-w-[80ch] text-[11.5px] text-ink-3">
        Capture is the share of labelled cells caught in the highest-scoring tenth of the grid; the base rate
        is the share that tenth would catch by chance. A model that only beats the base rate has learned the
        neighbourhood, not the rock.
      </p>
    </div>
  );
}

/** The one sentence the table is for. Which model it names is read off the numbers, never assumed. */
function SpatialFoldLine({ by }: { by: Map<string, string> }) {
  const effortId = by.get("effort.spatial.pr_auc" satisfies Key);
  const learnedId = by.get("learned.spatial.pr_auc" satisfies Key);
  const effort = numberOf(effortId);
  const learned = numberOf(learnedId);
  if (effortId === undefined || learnedId === undefined || effort === null || learned === null) return null;
  const effortLeads = effort > learned;
  return (
    <p className="mt-2 max-w-[80ch] text-[12px] text-ink-2" data-testid="spatial-fold-line">
      Under spatial folds the exploration-effort model scores <V id={effortId} /> on PR-AUC against the
      geological model's <V id={learnedId} />
      {effortLeads
        ? ": the ranking is better explained by where people have already drilled than by anything measured about the rock."
        : ": the geological model leads here, which is the only arrangement under which the ranking is about the rock."}
    </p>
  );
}
