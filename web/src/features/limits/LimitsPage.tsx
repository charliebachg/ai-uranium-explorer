import { Check, X } from "lucide-react";
import { useEffect, useState } from "react";
import { V } from "@/components/values/V";
import type { Readiness } from "@/data/contract";
import { loadReadiness } from "@/data/loader";

/**
 * What public data can and cannot support, one row per claim.
 */

const ROWS: { claim: string; credible: boolean; evidence: string; needed: string; measured?: string }[] = [
  {
    claim: "Reads collars and assays as printed",
    credible: true,
    evidence: "Grades exist only in the assessment files; the province publishes none.",
    needed: "A gold set keyed by hand.",
  },
  {
    claim: "Says what a grade or log means",
    credible: false,
    evidence: "Gamma logs misread uranium out of equilibrium; models read geological maps poorly.",
    needed: "A geologist and the company's QC data.",
  },
  {
    claim: "Ranks ground",
    credible: false,
    evidence: "Public labels sit where people looked, so a model learns exploration history.",
    needed: "Drill outcomes and geologist-chosen negatives.",
    measured: "ranks-ground",
  },
  {
    claim: "Says where to drill",
    credible: false,
    evidence: "The only quantified planner result is on synthetic deposits.",
    needed: "Predictions logged before drilling, over years.",
  },
  {
    claim: "An LLM judge checked it",
    credible: false,
    evidence: "Judge agreement falls far below raw agreement once chance is removed.",
    needed: "Geologist agreement measured first.",
  },
  {
    claim: "Anything on a company's own ground",
    credible: false,
    evidence: "Assessment files can stay confidential for three years.",
    needed: "Their data and permission.",
  },
];

/** What this system measured on its own output, rather than borrowing someone else's warning. */
function Measured({ readiness }: { readiness: Readiness | null }) {
  const rows = readiness?.metrics?.rows ?? [];
  const find = (model: string, fold: string, metric: string) =>
    rows.find((r) => r.model === model && r.fold === fold && r.metric === metric)?.value_id ?? null;
  const learned = find("learned", "spatial", "pr_auc");
  const effort = find("effort", "spatial", "pr_auc");
  if (!learned || !effort) return null;
  return (
    <div className="mt-1 text-[11.5px] text-st-flag" data-testid="limits-measured">
      Measured: geology <V id={learned} /> vs drilling history <V id={effort} /> PR-AUC, spatial folds.
    </div>
  );
}

export function LimitsPage() {
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  useEffect(() => {
    loadReadiness().then(setReadiness, () => undefined);
  }, []);
  return (
    <div className="h-full overflow-y-auto bg-ground" data-strict="limits">
      <div className="mx-auto max-w-[1000px] space-y-4 px-6 py-6">
        <header className="glass rounded-2xl px-4 py-2">
          <h1 className="font-semibold text-[15px] text-ink">Limits</h1>
          <p className="mt-0.5 text-[12px] text-ink-3">What public data can and cannot support.</p>
        </header>

        <section className="glass overflow-hidden rounded-2xl">
          <table className="w-full text-[12.5px]">
            <thead className="bg-white/[0.03] text-[10.5px] text-ink-3 uppercase tracking-wider">
              <tr>
                <th className="px-4 py-2 text-left font-normal">Claim</th>
                <th className="whitespace-nowrap px-2 py-2 text-left font-normal">Public data</th>
                <th className="px-4 py-2 text-left font-normal">Why</th>
                <th className="px-4 py-2 text-left font-normal">Needs</th>
              </tr>
            </thead>
            <tbody>
              {ROWS.map((r) => (
                <tr key={r.claim} className="border-line border-t align-top">
                  <td className="px-4 py-2 text-ink">{r.claim}</td>
                  <td className="px-2 py-2">
                    {r.credible ? (
                      <span className="inline-flex items-center gap-1 rounded-full bg-src-geods/15 px-2 py-0.5 text-[11px] text-src-geods">
                        <Check className="size-3" /> yes
                      </span>
                    ) : (
                      <span className="inline-flex items-center gap-1 rounded-full bg-st-miss/15 px-2 py-0.5 text-[11px] text-st-miss">
                        <X className="size-3" /> no
                      </span>
                    )}
                  </td>
                  <td className="px-4 py-2 text-ink-2">
                    <span data-source-text>{r.evidence}</span>
                    {r.measured === "ranks-ground" ? <Measured readiness={readiness} /> : null}
                  </td>
                  <td className="px-4 py-2 text-ink-3">{r.needed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>
      </div>
    </div>
  );
}
