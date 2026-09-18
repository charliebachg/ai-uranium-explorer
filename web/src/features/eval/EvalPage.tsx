import { AlertTriangle, ArrowUpRight, FlaskConical } from "lucide-react";
import { useEffect, useState } from "react";
import { useLocation } from "wouter";
import { Chip } from "@/components/ui/StatusMark";
import { V } from "@/components/values/V";
import type { Readiness, RunSummary } from "@/data/contract";
import { loadReadiness, loadRunSummary } from "@/data/loader";
import { resolveValue } from "@/data/registry";
import { MetricsStrip } from "@/features/prospect/MetricsStrip";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";

/**
 * What this run did, and what it does not say. With no gold labels there is no accuracy figure, so the page
 * shows run statistics and states the missing denominator at the top.
 */
export function EvalPage() {
  const [data, setData] = useState<RunSummary | null>(null);
  const [readiness, setReadiness] = useState<Readiness | null>(null);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => {
    loadRunSummary().then(setData, (e: unknown) => setError(String(e)));
    loadReadiness().then(setReadiness, () => setReadiness(null));
  }, []);

  if (error)
    return (
      <Shell>
        <div className="text-[13px] text-st-miss">{error}</div>
      </Shell>
    );
  if (!data)
    return (
      <Shell>
        <div className="text-[13px] text-ink-3">Loading run statistics…</div>
      </Shell>
    );

  return (
    <Shell>
      <ProvenanceStrip data={data} />

      <div className="grid grid-cols-3 gap-3">
        <Card title="Read" tone="plain">
          <Big id={data.coverage.pages_read} unit="pages" />
          <Line label="assessment files" id={data.coverage.files_read} />
          <Line label="tables" id={data.coverage.tables} />
          <Line label="holes assembled" id={data.coverage.holes} />
        </Card>
        <Card title="Values with a quote on the page" tone="plain">
          <Big id={data.reading.located_share} unit="of printed values" />
          <Line label="printed values" id={data.reading.printed_values} />
          <Line label="quote located" id={data.reading.located} />
          <Line label="cells empty on the page" id={data.reading.empty_cells} />
          <Line label="of those, boxed" id={data.reading.empty_cells_boxed} />
        </Card>
        <Card title="Second reader (OCR)" tone="plain">
          <Big id={data.reading.digit_mismatch} unit="values differ" />
          <Line label="digits agree" id={data.reading.digit_exact} />
          <Line label="agree after folding confusables" id={data.reading.digit_confusable} />
          <p className="mt-2 text-[11.5px] text-ink-3">
            Differing readers, not a wrong transcription; here the OCR is often at fault.
          </p>
        </Card>
      </div>

      <Section
        title="What the checks flagged"
        hint="Nothing is dropped; every value keeps its flags. Click an example to open its page."
      >
        <ul className="space-y-2">
          {data.checks.map((c) => (
            <CheckRow key={c.id} check={c} />
          ))}
        </ul>
      </Section>

      <div className="grid grid-cols-2 gap-3">
        <Card title="Where the holes came from" tone="plain">
          <Line label="from coordinates on the page" id={data.placement.from_page} />
          <Line label="from a provincial position" id={data.placement.from_provincial} />
          <Line label="not placed" id={data.placement.not_placed} />
          <p className="mt-2 text-[11.5px] text-ink-3">
            Only the first row tests the reading; the rest say where the hole is, not how well it was read.
          </p>
        </Card>
        <Card title="Cross-check against provincial records" tone="plain">
          <Line label="provincial records matched" id={data.crosscheck.matches} />
          <Line label="of those, independent" id={data.crosscheck.independent} />
          <Line label="median independent offset" id={data.crosscheck.median_offset_m} />
          <Line label="largest independent offset" id={data.crosscheck.max_offset_m} />
          <Line label="offsets like a datum shift" id={data.crosscheck.datum_shift_signatures} />
        </Card>
      </div>

      <Section title="Per file">
        <table className="w-full text-[12.5px]">
          <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
            <tr>
              <th className="py-1.5 text-left font-normal">File</th>
              <th className="py-1.5 text-right font-normal">Printed values</th>
              <th className="py-1.5 text-right font-normal">Quote located</th>
              <th className="py-1.5 text-right font-normal">Tables</th>
              <th className="py-1.5 text-right font-normal">Holes</th>
            </tr>
          </thead>
          <tbody>
            {data.per_file.map((f) => (
              <tr key={f.file_num} className="border-line border-t">
                <td className="py-1.5" data-ident>
                  {f.file_num}
                </td>
                <td className="py-1.5 text-right">
                  <V id={f.values} />
                </td>
                <td className="py-1.5 text-right">
                  <V id={f.located} />
                </td>
                <td className="py-1.5 text-right">
                  <V id={f.tables} />
                </td>
                <td className="py-1.5 text-right">
                  <V id={f.holes} />
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </Section>

      <Section title="What this run cost">
        <div className="flex flex-wrap gap-x-8 gap-y-2 text-[12.5px]">
          <Line label="model calls" id={data.run.calls} />
          <Line label="list-price equivalent" id={data.run.cost_usd} prefix="$" />
          <Line label="input tokens" id={data.run.tokens_in} />
          <Line label="output tokens" id={data.run.tokens_out} />
          <Line label="model time (minutes)" id={data.run.minutes} />
        </div>
        <p className="mt-2 text-[11.5px] text-ink-3">
          Model: <span data-ident>{data.run.models.join(", ")}</span>. One page per call, read once and
          cached.
        </p>
      </Section>

      <Section
        title="What would turn these into accuracy"
        hint="Until then: what was read and what the checks caught, not how often the reading is right."
      >
        <ol className="list-inside list-decimal space-y-1 text-[12.5px] text-ink-2">
          <li>Key whole pages by hand, blind to the model output.</li>
          <li>Count printed rows first, so values never returned count as misses.</li>
          <li>Hold out files; score once, after the configuration is frozen.</li>
          <li>Report recall separately, with a class-A miss rate and an interval.</li>
        </ol>
      </Section>

      <ScoresSection readiness={readiness} />
      <GateSection readiness={readiness} />
    </Shell>
  );
}

/**
 * The one number on this page with a real denominator: how often the check that guards the screen is right,
 * on a suite built to defeat it.
 */
/** What the map's three scores are worth, measured under folds that keep a camp together. */
function ScoresSection({ readiness }: { readiness: Readiness | null }) {
  if (!readiness?.metrics?.rows?.length) return null;
  return (
    <Section
      title="What the map's scores are worth"
      hint="Measured on the labelled cells the models were scored against. The spatial-fold row is the honest one."
    >
      <MetricsStrip data={readiness} />
    </Section>
  );
}

function GateSection({ readiness }: { readiness: Readiness | null }) {
  const gate = readiness?.gate;
  if (!gate) return null;
  const missedOf = (id: string) => Number(resolveValue(id)?.value ?? 0);
  const kinds = Object.entries(gate.by_kind).sort((a, b) => missedOf(b[1].missed) - missedOf(a[1].missed));
  return (
    <Section
      title="The fabrication gate, put to the test"
      hint="Real evidence packs, true claims, and the same claims corrupted the way a model plausibly would. No model involved, so it runs on every change."
    >
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2" data-testid="gate-cards">
        <Card title="Fabricated claims refused" tone="plain">
          <Big id={gate.caught_rate} unit="of corrupted claims" />
          <Line label="corrupted claims put to it" id={gate.put} />
          <Line label="refused" id={gate.caught} />
          <Line label="got through" id={gate.missed} />
        </Card>
        <Card title="True claims wrongly refused" tone="plain">
          <Big id={gate.wrongly_rejected} unit="of true claims" />
          <Line label="true claims put to it" id={gate.honest} />
          <p className="mt-2 text-[11.5px] text-ink-3">
            A gate that refuses honest prose gets switched off, so both directions are measured.
          </p>
        </Card>
      </div>

      <table className="mt-3 w-full text-[12px]" data-testid="gate-kinds">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="py-1 text-left font-normal">How the claim was corrupted</th>
            <th className="py-1 text-right font-normal">refused</th>
            <th className="py-1 text-right font-normal">got through</th>
          </tr>
        </thead>
        <tbody>
          {kinds.map(([kind, row]) => (
            <tr key={kind} className="border-line border-t" data-testid="gate-kind-row">
              <td className="py-1.5 text-ink-2">{KIND_LABEL[kind] ?? kind}</td>
              <td className="py-1.5 text-right">
                <V id={row.caught} className="text-ink-2" />
              </td>
              <td className="py-1.5 text-right">
                <V id={row.missed} className={missedOf(row.missed) ? "text-st-flag" : "text-ink-3"} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <p className="mt-2.5 max-w-[86ch] text-[11.5px] text-ink-3">
        This measures the check, not how often a model writes such a claim — that needs the model, and is the
        denominator problem stated at the top. The claims that get through are small numbers that also appear
        in text the agent may quote back.
      </p>
    </Section>
  );
}

/** Plain English for the corruption each case applied, since the keys are written for the suite, not the page. */
const KIND_LABEL: Record<string, string> = {
  stated_plainly: "stated plainly (true)",
  digit_slip: "one digit changed",
  decimal_shift: "the decimal point moved",
  transposed: "two digits swapped",
  false_precision: "precision the value does not have",
  hand_conversion: "metres to kilometres done by hand",
  invented: "a plausible figure invented outright",
  cited_to_the_wrong_value: "a real number cited to the wrong value",
  uncited: "a real number cited to nothing",
};

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-full overflow-y-auto bg-ground" data-strict="eval">
      <div className="mx-auto max-w-[1100px] space-y-4 px-6 py-6">{children}</div>
    </div>
  );
}

function ProvenanceStrip({ data }: { data: RunSummary }) {
  return (
    <div className="glass sticky top-0 z-10 rounded-2xl border-st-flag/30 px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="flex items-center gap-2 rounded-lg bg-st-flag/15 px-2.5 py-1 text-[12px] text-st-flag">
          <FlaskConical className="size-3.5" /> No gold labels yet: these are run statistics, not accuracy
        </span>
        <span className="text-[12px] text-ink-3">
          run <span data-ident>{data.run.run_ids.join(", ") || "none"}</span> · pipeline{" "}
          <span data-ident>{data.pipeline_version}</span> · generated{" "}
          <span data-chrome>{data.generated_at.slice(0, 16).replace("T", " ")}</span>
        </span>
      </div>
      {data.caveats.length ? (
        <details className="mt-1.5">
          <summary className="cursor-pointer text-[11.5px] text-ink-3 hover:text-ink-2">
            <AlertTriangle className="-mt-0.5 mr-1.5 inline size-3" aria-hidden="true" />
            What these numbers do not say
          </summary>
          <ul className="mt-1.5 space-y-1 border-line border-l pl-3">
            {data.caveats.map((c) => (
              <li key={c} className="max-w-[86ch] text-[11.5px] text-ink-3">
                {c}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

function CheckRow({ check }: { check: RunSummary["checks"][number] }) {
  const openValue = useStore((s) => s.openValue);
  const openHole = useStore((s) => s.openHole);
  const [, navigate] = useLocation();
  const classA = resolveValue(check.class_a)?.value;
  const hasClassA = typeof classA === "number" && classA > 0;
  return (
    <li className={cn("rounded-xl border border-line bg-black/20 p-3", hasClassA && "border-st-miss/30")}>
      <div className="flex flex-wrap items-center gap-2">
        <span className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[11px] text-ink-2" data-ident>
          {check.id}
        </span>
        <span className="text-[13px] text-ink">{check.title}</span>
        <span className="ml-auto flex items-center gap-2">
          {hasClassA ? (
            <Chip tone="miss">
              <V id={check.class_a} /> class A
            </Chip>
          ) : null}
          <Chip tone={check.severity === "error" ? "miss" : "flag"}>
            <V id={check.flags} /> flagged
          </Chip>
        </span>
      </div>
      {check.examples.length ? (
        <ul className="mt-2 space-y-1">
          {check.examples.map((ex) => (
            <li key={ex.value_id}>
              <button
                type="button"
                onClick={() => {
                  openHole(ex.file_num, null);
                  openValue(ex.value_id, ex.page ? { file: ex.file_num, page: ex.page } : undefined);
                  navigate("/");
                }}
                className="group flex w-full items-start gap-2 rounded-lg px-2 py-1.5 text-left text-[12px] text-ink-2 hover:bg-white/[0.04]"
              >
                <span className="text-ink-3" data-ident>
                  {ex.file_num}
                </span>
                <span className="min-w-0 flex-1" data-source-text>
                  {ex.message}
                </span>
                <ArrowUpRight className="mt-0.5 size-3.5 shrink-0 text-ink-3 opacity-0 group-hover:opacity-100" />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </li>
  );
}

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="glass rounded-2xl p-4">
      <h2 className="text-[13px] text-ink">{title}</h2>
      {hint ? <p className="mt-0.5 mb-2 text-[11.5px] text-ink-3">{hint}</p> : <div className="mb-2" />}
      {children}
    </section>
  );
}

function Card({ title, children }: { title: string; tone?: "plain"; children: React.ReactNode }) {
  return (
    <div className="glass rounded-2xl p-4">
      <div className="text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">{title}</div>
      <div className="mt-2 space-y-1">{children}</div>
    </div>
  );
}

function Big({ id, unit }: { id: string; unit: string }) {
  return (
    <div className="flex items-baseline gap-2">
      <span className="text-[30px] text-ink leading-none">
        <V id={id} />
      </span>
      <span className="text-[12px] text-ink-3">{unit}</span>
    </div>
  );
}

function Line({ label, id, prefix }: { label: string; id: string; prefix?: string }) {
  return (
    <div className="flex items-baseline justify-between gap-3 text-[12.5px]">
      <span className="text-ink-3">{label}</span>
      <span className="text-ink-2">
        {prefix ? <span className="text-ink-3">{prefix}</span> : null}
        <V id={id} />
      </span>
    </div>
  );
}
