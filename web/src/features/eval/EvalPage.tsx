import { ArrowUpRight, ChevronRight, FlaskConical } from "lucide-react";
import { useEffect, useState } from "react";
import { useLocation } from "wouter";
import { Chip } from "@/components/ui/StatusMark";
import { V } from "@/components/values/V";
import type { Readiness, RunSummary } from "@/data/contract";
import { loadReadiness, loadRunSummary } from "@/data/loader";
import { resolveValue } from "@/data/registry";
import { BenchSection, HeadlineSection, HindcastSection, SearchSection } from "@/features/eval/PhaseSections";
import { MetricsStrip } from "@/features/prospect/MetricsStrip";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";

/**
 * The evaluations, results first: the analyst benchmark, what the map's scores are worth, the hindcast, the
 * fabrication gate, and last the reading run, which has no gold labels and so reports counts, not accuracy.
 * Every section is a table or a tile; what a column means is on its header, the method is in the GUIDE.
 */

const NAV: [string, string][] = [
  ["benchmark", "Benchmark"],
  ["scores", "Map scores"],
  ["hindcast", "Hindcast"],
  ["gate", "Gate"],
  ["reading", "Reading"],
];

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
        <div className="text-[13px] text-ink-3">Loading…</div>
      </Shell>
    );

  return (
    <Shell>
      <Header data={data} />
      <BenchSection block={readiness?.bench} />
      <ScoresSection readiness={readiness} />
      <HeadlineSection block={readiness?.headline} />
      <SearchSection block={readiness?.search} />
      <HindcastSection block={readiness?.hindcast} />
      <GateSection readiness={readiness} />
      <ReadingSection data={data} />
    </Shell>
  );
}

function Header({ data }: { data: RunSummary }) {
  return (
    <div className="glass sticky top-0 z-10 flex flex-wrap items-center gap-x-4 gap-y-2 rounded-2xl px-4 py-2.5">
      <h1 className="font-semibold text-[15px] text-ink">Eval</h1>
      <nav className="flex flex-wrap gap-1" aria-label="Sections">
        {NAV.map(([id, label]) => (
          <button
            key={id}
            type="button"
            onClick={() =>
              document.getElementById(id)?.scrollIntoView({ behavior: "smooth", block: "start" })
            }
            className="rounded-lg px-2 py-1 text-[12px] text-ink-3 hover:bg-white/[0.05] hover:text-ink-2"
          >
            {label}
          </button>
        ))}
      </nav>
      <span className="ml-auto text-[11.5px] text-ink-3">
        pipeline <span data-ident>{data.pipeline_version}</span> ·{" "}
        <span data-chrome>{data.generated_at.slice(0, 16).replace("T", " ")}</span>
      </span>
    </div>
  );
}

function ScoresSection({ readiness }: { readiness: Readiness | null }) {
  if (!readiness?.metrics?.rows?.length) return null;
  return (
    <Section
      id="scores"
      title="Map scores"
      hint="Out of fold on the labelled cells. Spatial folds are the fair test."
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
      id="gate"
      title="Fabrication gate"
      hint="True claims and corrupted copies over real evidence packs. No model call."
    >
      <div className="grid grid-cols-1 gap-3 md:grid-cols-2" data-testid="gate-cards">
        <Card title="Fabrications refused">
          <Big id={gate.caught_rate} />
          <Line label="put" id={gate.put} />
          <Line label="refused" id={gate.caught} />
          <Line label="through" id={gate.missed} />
        </Card>
        <Card title="True claims refused">
          <Big id={gate.wrongly_rejected} />
          <Line label="put" id={gate.honest} />
        </Card>
      </div>

      <table className="mt-3 w-full text-[12px]" data-testid="gate-kinds">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="py-1 text-left font-normal">Corruption</th>
            <th className="py-1 text-right font-normal">Refused</th>
            <th className="py-1 text-right font-normal">Through</th>
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
    </Section>
  );
}

/** Plain English for the corruption each case applied, since the keys are written for the suite, not the page. */
const KIND_LABEL: Record<string, string> = {
  stated_plainly: "none (true claim)",
  digit_slip: "one digit changed",
  decimal_shift: "decimal point moved",
  transposed: "two digits swapped",
  false_precision: "false precision",
  hand_conversion: "unit converted by hand",
  invented: "figure invented",
  cited_to_the_wrong_value: "cited to the wrong value",
  uncited: "cited to nothing",
};

/** The reading run: counts and flags, with no gold page keyed, so nothing here is an accuracy. */
function ReadingSection({ data }: { data: RunSummary }) {
  const runs = data.run.run_ids;
  return (
    <Section
      id="reading"
      title="Reading"
      aside={
        <Chip tone="flag">
          <FlaskConical className="size-3" /> No gold set: counts, not accuracy
        </Chip>
      }
    >
      <div className="grid grid-cols-3 gap-3">
        <Card title="Pages read">
          <Big id={data.coverage.pages_read} />
          <Line label="files" id={data.coverage.files_read} />
          <Line label="tables" id={data.coverage.tables} />
          <Line label="holes" id={data.coverage.holes} />
        </Card>
        <Card title="Quote located">
          <Big id={data.reading.located_share} />
          <Line label="printed values" id={data.reading.printed_values} />
          <Line label="located" id={data.reading.located} />
          <Line label="empty cells" id={data.reading.empty_cells} />
          <Line label="empty, boxed" id={data.reading.empty_cells_boxed} />
        </Card>
        <Card title="OCR disagrees">
          <Big id={data.reading.digit_mismatch} />
          <Line label="digits agree" id={data.reading.digit_exact} />
          <Line label="agree after confusables" id={data.reading.digit_confusable} />
        </Card>
      </div>

      <h3 className="mt-4 mb-1 text-[12px] text-ink-2">Checks</h3>
      <div data-testid="checks">
        {data.checks.map((c) => (
          <CheckRow key={c.id} check={c} />
        ))}
      </div>

      <div className="mt-4 grid grid-cols-2 gap-3">
        <Card title="Hole positions">
          <Line label="from the page" id={data.placement.from_page} />
          <Line label="from provincial records" id={data.placement.from_provincial} />
          <Line label="not placed" id={data.placement.not_placed} />
        </Card>
        <Card title="Provincial cross-check">
          <Line label="records matched" id={data.crosscheck.matches} />
          <Line label="independent" id={data.crosscheck.independent} />
          <Line label="median offset" id={data.crosscheck.median_offset_m} />
          <Line label="largest offset" id={data.crosscheck.max_offset_m} />
          <Line label="datum-shift offsets" id={data.crosscheck.datum_shift_signatures} />
        </Card>
      </div>

      <div className="mt-3 flex flex-wrap gap-x-6 gap-y-1 text-[12.5px]" data-testid="run-cost">
        <Line label="calls" id={data.run.calls} />
        <Line label="cost" id={data.run.cost_usd} prefix="$" />
        <Line label="tokens in" id={data.run.tokens_in} />
        <Line label="tokens out" id={data.run.tokens_out} />
        <Line label="model minutes" id={data.run.minutes} />
        <span className="text-ink-3" data-ident>
          {data.run.models.join(", ")}
        </span>
      </div>

      <Disclosure label="Per file" testId="per-file">
        <table className="w-full text-[12.5px]">
          <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
            <tr>
              <th className="py-1.5 text-left font-normal">File</th>
              <th className="py-1.5 text-right font-normal">Values</th>
              <th className="py-1.5 text-right font-normal">Located</th>
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
      </Disclosure>
      <Disclosure
        label={
          runs.length ? (
            <>
              Tracked runs · <span data-instrument>{runs.length}</span>
            </>
          ) : (
            "No tracked run"
          )
        }
        testId="run-ids"
      >
        <p className="text-[11.5px] text-ink-3" data-ident>
          {runs.join(", ") || "none"}
        </p>
      </Disclosure>
      {data.caveats.length ? (
        <Disclosure label="Notes">
          <ul className="space-y-1">
            {data.caveats.map((c) => (
              <li key={c} className="max-w-[86ch] text-[11.5px] text-ink-3" data-source-text>
                {c}
              </li>
            ))}
          </ul>
        </Disclosure>
      ) : null}
    </Section>
  );
}

/** One check: its id, what it tests and its counts on one line; the examples open under it. */
function CheckRow({ check }: { check: RunSummary["checks"][number] }) {
  const openValue = useStore((s) => s.openValue);
  const openHole = useStore((s) => s.openHole);
  const [, navigate] = useLocation();
  const classA = resolveValue(check.class_a)?.value;
  const hasClassA = typeof classA === "number" && classA > 0;
  return (
    <details className="group border-line border-t" data-testid="check-row" data-check={check.id}>
      <summary className="flex cursor-pointer list-none items-center gap-2 py-1.5 text-[12.5px] hover:bg-white/[0.03]">
        <ChevronRight className="size-3.5 shrink-0 text-ink-3 transition-transform group-open:rotate-90" />
        <span className="w-9 shrink-0 font-mono text-[11px] text-ink-3" data-ident>
          {check.id}
        </span>
        <span className="min-w-0 flex-1 truncate text-ink-2">{check.title}</span>
        {hasClassA ? (
          <Chip tone="miss">
            <V id={check.class_a} /> class A
          </Chip>
        ) : null}
        <Chip tone={check.severity === "error" ? "miss" : "flag"}>
          <V id={check.flags} /> flagged
        </Chip>
      </summary>
      {check.examples.length ? (
        <ul className="mb-2 ml-6">
          {check.examples.map((ex) => (
            <li key={ex.value_id}>
              <button
                type="button"
                onClick={() => {
                  openHole(ex.file_num, null);
                  openValue(ex.value_id, ex.page ? { file: ex.file_num, page: ex.page } : undefined);
                  navigate("/");
                }}
                className="group/ex flex w-full items-start gap-2 rounded-lg px-2 py-1 text-left text-[12px] text-ink-2 hover:bg-white/[0.04]"
              >
                <span className="text-ink-3" data-ident>
                  {ex.file_num}
                </span>
                <span className="min-w-0 flex-1" data-source-text>
                  {ex.message}
                </span>
                <ArrowUpRight className="mt-0.5 size-3.5 shrink-0 text-ink-3 opacity-0 group-hover/ex:opacity-100" />
              </button>
            </li>
          ))}
        </ul>
      ) : null}
    </details>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-full overflow-y-auto bg-ground" data-strict="eval">
      <div className="mx-auto max-w-[1100px] space-y-4 px-6 py-6">{children}</div>
    </div>
  );
}

export function Section({
  id,
  title,
  hint,
  aside,
  children,
}: {
  id?: string;
  title: React.ReactNode;
  hint?: string;
  aside?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section id={id} className="glass scroll-mt-16 rounded-2xl p-4">
      <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        <h2 className="text-[13px] text-ink">{title}</h2>
        {aside ? (
          <div className="ml-auto flex items-center gap-2 text-[11.5px] text-ink-3">{aside}</div>
        ) : null}
      </div>
      {hint ? <p className="mt-0.5 mb-2 text-[11.5px] text-ink-3">{hint}</p> : <div className="mb-2" />}
      {children}
    </section>
  );
}

/** A closed-by-default block under a section: detail a reader asks for, not the first thing they see. */
export function Disclosure({
  label,
  testId,
  children,
}: {
  label: React.ReactNode;
  testId?: string;
  children: React.ReactNode;
}) {
  return (
    <details className="group mt-2" data-testid={testId}>
      <summary className="flex cursor-pointer list-none items-center gap-1 text-[11.5px] text-ink-3 hover:text-ink-2">
        <ChevronRight className="size-3 transition-transform group-open:rotate-90" />
        {label}
      </summary>
      <div className="mt-1.5 border-line border-l pl-3">{children}</div>
    </details>
  );
}

function Card({ title, children }: { title: string; children: React.ReactNode }) {
  return (
    <div className="rounded-xl border border-line bg-black/20 p-3">
      <div className="text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">{title}</div>
      <div className="mt-1.5 space-y-1">{children}</div>
    </div>
  );
}

function Big({ id }: { id: string }) {
  return (
    <div className={cn("text-[28px] text-ink leading-none")}>
      <V id={id} />
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
