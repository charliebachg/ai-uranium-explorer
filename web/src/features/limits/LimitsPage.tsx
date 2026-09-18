import { Check, X } from "lucide-react";
import { useEffect, useState } from "react";
import { V } from "@/components/values/V";
import { WORDING } from "@/config/wording";
import type { Readiness } from "@/data/contract";
import { loadReadiness } from "@/data/loader";

/**
 * What a public-data demo can and cannot claim, carried from research report 05 (section 4, Tables 4.1 and 4.2),
 * where each line is cited to its source. The honest position is also the more persuasive one.
 */

const ROWS: { claim: string; credible: boolean; evidence: string; needed: string; measured?: string }[] = [
  {
    claim: "It reads collars and assay intervals as printed",
    credible: true,
    evidence:
      "The province publishes no assay values, so grades exist only inside the assessment files, and no published accuracy figure exists for extracting them. A measured number here would be new.",
    needed: "A gold set keyed by hand; a geologist only for interpretive fields.",
  },
  {
    claim: "This grade or log means X",
    credible: false,
    evidence:
      "Where uranium is out of balance with the decay products that emit the gamma rays, a downhole gamma log gives an incorrect estimate of the uranium. Frontier models also score poorly at reading geological maps.",
    needed: "A geologist, plus the company's own quality-control data.",
  },
  {
    claim: "This map ranks ground",
    credible: false,
    evidence:
      "Known deposits sit where people looked, so a model trained on public labels learns exploration history. Prospectivity scores fall sharply once folds are drawn so nearby cells cannot leak between training and test.",
    needed: "Drill outcomes, geologist-chosen negatives, and folds fixed before modelling.",
    measured: "ranks-ground",
  },
  {
    claim: "Drill here, or this saves holes",
    credible: false,
    evidence:
      "The one quantified planner result comes from synthetic two-dimensional deposits, where it wrongly abandoned about a third of profitable multi-body cases. Developed uranium discoveries waited an average of 14.1 years.",
    needed: "Years of predictions logged before drilling.",
  },
  {
    claim: "An LLM judge checked it",
    credible: false,
    evidence:
      "Across 21 judges, agreement corrected for chance ran far below raw agreement, so a judge that looks accurate can be agreeing by chance.",
    needed: "Geologist-to-geologist agreement measured first, then the judge scored against it.",
  },
  {
    claim: "Anything about a company's own ground",
    credible: false,
    evidence: "Saskatchewan assessment files can stay confidential for three years from submission.",
    needed: "Their data, and their permission.",
  },
];

const SCRIPT: { moment: string; say: string }[] = [
  { moment: "Opening", say: WORDING.opening },
  {
    moment: "Map banner",
    say: WORDING.banner,
  },
  {
    moment: "Showing a grade",
    say: "This is the value as printed, with the unit and basis the report states. What it means for the deposit is a geologist's call.",
  },
  {
    moment: 'Asked "could it find targets?"',
    say: "Not from public data, and not by me. Deposit labels are few and sit where people drilled, so a map here would learn exploration history. That needs your geologists and drill results.",
  },
  {
    moment: "Scorecard",
    say: "No gold set has been labelled yet, so nothing here is an accuracy figure: these are run statistics and what the checks caught. Once a sample is keyed by hand, the same page reports recall first, then a class-A miss rate with an interval.",
  },
  {
    moment: "Closing",
    say: "To trust this for drilling, your geologists would define which errors matter and label a sample. That is my first ask.",
  },
];

/** What this project measured on its own output, rather than borrowing someone else's warning. */
function Measured({ readiness }: { readiness: Readiness | null }) {
  const rows = readiness?.metrics?.rows ?? [];
  const find = (model: string, fold: string, metric: string) =>
    rows.find((r) => r.model === model && r.fold === fold && r.metric === metric)?.value_id ?? null;
  const learned = find("learned", "spatial", "pr_auc");
  const effort = find("effort", "spatial", "pr_auc");
  const criteriaRoc = find("criteria", "none", "roc_auc");
  const criteriaCapture = find("criteria", "none", "capture_top10");
  if (!learned || !effort) return null;
  return (
    <div className="mt-2 rounded-lg border border-st-flag/25 bg-st-flag/[0.06] px-3 py-2 text-[12px]">
      <div className="mb-1 text-[10.5px] text-st-flag uppercase tracking-wider">Measured here</div>
      <p className="text-ink-2 leading-relaxed">
        On this grid, under spatial folds, the model trained on geology scores <V id={learned} /> (PR-AUC)
        against <V id={effort} /> for a model given nothing but where people already drilled. The
        knowledge-driven criteria score, which is not fitted to the labels at all, reaches{" "}
        {criteriaRoc ? <V id={criteriaRoc} /> : null} ROC-AUC and captures{" "}
        {criteriaCapture ? <V id={criteriaCapture} /> : null} of known positives in the top tenth of the area.
        Exploration history predicts the labels better than the geology does.
      </p>
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
        <header className="glass rounded-2xl p-5">
          <h1 className="font-semibold text-[18px] text-ink tracking-tight">
            What this demo can and cannot claim
          </h1>
          <p className="mt-1.5 max-w-[74ch] text-[13px] text-ink-2">
            Public records can be turned into checked, traceable tables, and the reading can be measured. They
            cannot show that any output is good geology: that evidence is geologist judgement and drill
            outcomes, and neither is public.
          </p>
          <p className="mt-2 text-[11.5px] text-ink-3">
            Carried from this project's <span data-ident>research report 05, section 4</span>.
          </p>
        </header>

        <section className="glass overflow-hidden rounded-2xl">
          <table className="w-full text-[12.5px]">
            <thead className="bg-white/[0.03] text-[10.5px] text-ink-3 uppercase tracking-wider">
              <tr>
                <th className="px-4 py-2 text-left font-normal">Claim</th>
                <th className="px-2 py-2 text-left font-normal">On public data</th>
                <th className="px-4 py-2 text-left font-normal">Why</th>
                <th className="px-4 py-2 text-left font-normal">What it would take</th>
              </tr>
            </thead>
            <tbody>
              {ROWS.map((r) => (
                <tr key={r.claim} className="border-line border-t align-top">
                  <td className="px-4 py-3 text-ink">{r.claim}</td>
                  <td className="px-2 py-3">
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
                  {/* carried verbatim from research report 05, where each line is cited to its source */}
                  <td className="px-4 py-3 text-ink-2">
                    <span data-source-text>{r.evidence}</span>
                    {r.measured === "ranks-ground" ? <Measured readiness={readiness} /> : null}
                  </td>
                  <td className="px-4 py-3 text-ink-3">{r.needed}</td>
                </tr>
              ))}
            </tbody>
          </table>
        </section>

        <section className="glass rounded-2xl p-5">
          {/* presenter notes: useful to have, not something an audience should have to read past */}
          <details>
            <summary className="cursor-pointer text-[13px] text-ink hover:text-ink-2">
              How to say it out loud
            </summary>
            <dl className="mt-3 space-y-3">
              {SCRIPT.map((s) => (
                <div key={s.moment} className="grid grid-cols-[150px_1fr] gap-4">
                  <dt className="text-[11.5px] text-ink-3 uppercase tracking-wider">{s.moment}</dt>
                  <dd className="text-[13px] text-ink-2 leading-relaxed">"{s.say}"</dd>
                </div>
              ))}
            </dl>
          </details>
        </section>
      </div>
    </div>
  );
}
