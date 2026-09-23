import { Tip } from "@/components/ui/Tip";
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
  effort: "Effort (null)",
};

const FOLD_LABEL: Record<MetricRow["fold"], string> = {
  none: "none (in sample)",
  random: "random",
  spatial: "spatial",
  camp: "camp",
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
    return <p className="text-[11.5px] text-ink-3">No metric table in the export.</p>;
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
            <th className="py-1.5 text-left font-normal">Folds</th>
            <th className="w-[92px] py-1.5 text-right font-normal">PR-AUC</th>
            <th className="w-[112px] py-1.5 text-right font-normal">
              <Tip
                text="Share of labelled cells in the top-scoring tenth of the grid."
                className="border-line-strong border-b border-dotted"
              >
                <span data-chrome>capture@10%</span>
              </Tip>
            </th>
            <th className="w-[92px] py-1.5 text-right font-normal">
              <Tip
                text="Share of labelled cells a random tenth of the grid would catch."
                className="border-line-strong border-b border-dotted"
              >
                Base rate
              </Tip>
            </th>
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
    <p className="mt-2 text-[12px] text-ink-2" data-testid="spatial-fold-line">
      Spatial folds: effort <V id={effortId} /> vs geology <V id={learnedId} />.{" "}
      <span className={effortLeads ? "text-st-flag" : "text-st-pass"}>
        {effortLeads ? "Drilling history ranks better than the geology." : "Geology ranks better."}
      </span>
    </p>
  );
}
