import { AlertTriangle, ArrowUpRight, Check, Layers, X } from "lucide-react";
import { useEffect, useMemo, useState } from "react";
import { Chip } from "@/components/ui/StatusMark";
import { V } from "@/components/values/V";
import type { ProspectFeature, ProspectGap, ProspectSource, Readiness, ReadinessGate } from "@/data/contract";
import { loadReadiness } from "@/data/loader";
import { resolveValue } from "@/data/registry";
import { cn } from "@/lib/cn";
import { CoverageMap } from "./CoverageMap";
import { recordCountId, registerRecordCounts } from "./recordCounts";

/**
 * What the data can support, before anything is scored. Each row answers one question: how much of the study
 * area does this measurement cover? The gaps at the foot of the page carry as much weight as the table above.
 */
export function ReadinessPage() {
  const [data, setData] = useState<Readiness | null>(null);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    loadReadiness().then(
      (r) => {
        registerRecordCounts(r.sources);
        setData(r);
      },
      (e: unknown) => setError(String(e)),
    );
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
        <Skeleton />
      </Shell>
    );

  return (
    <Shell>
      <HeaderStrip data={data} />
      <Totals data={data} />
      <FeatureSection data={data} />
      <Section title="Where the observations are" hint="One dot per grid cell, from the exported cell file.">
        <CoverageMap
          cellMetres={numberOf(data.grid.cell_m, 2000)}
          cellSizeId={data.grid.cell_m}
          geoFeaturesId={data.totals.geo_features}
        />
      </Section>
      <GateSection gate={data.readiness_gate} />
      <SourcesSection sources={data.sources} />
      <GapsSection gaps={data.gaps} totalId={data.totals.gaps} />
    </Shell>
  );
}

function Shell({ children }: { children: React.ReactNode }) {
  return (
    <div className="h-full overflow-y-auto bg-ground" data-strict="readiness" data-testid="readiness">
      <div className="mx-auto max-w-[1100px] space-y-4 px-6 py-6">{children}</div>
    </div>
  );
}

/** A value's number, for layout only (bar widths and sort order); nothing is printed from it. */
function numberOf(id: string | null, fallback = 0): number {
  const v = id ? resolveValue(id)?.value : null;
  return typeof v === "number" ? v : fallback;
}

function Skeleton() {
  return (
    <div className="space-y-3" data-testid="readiness-skeleton">
      <div className="h-20 animate-pulse rounded-2xl bg-white/[0.04]" />
      <div className="h-24 animate-pulse rounded-2xl bg-white/[0.03]" />
      <div className="h-80 animate-pulse rounded-2xl bg-white/[0.03]" />
    </div>
  );
}

// ---------- header ----------

function HeaderStrip({ data }: { data: Readiness }) {
  return (
    <div className="glass sticky top-0 z-10 rounded-2xl border-st-flag/30 px-4 py-3">
      <div className="flex flex-wrap items-center gap-3">
        <span className="flex items-center gap-2 rounded-lg bg-st-flag/15 px-2.5 py-1 text-[12px] text-st-flag">
          <Layers className="size-3.5" /> Coverage, not prospectivity
        </span>
        <span className="flex flex-wrap items-center gap-x-3 gap-y-1 text-[12px] text-ink-3">
          <Fact label="cells" id={data.grid.cells} />
          <Fact label="cell size" id={data.grid.cell_m} />
          <Fact label="area" id={data.grid.area_km2} />
          <Fact label="over the sandstone" id={data.grid.in_basin} />
          <Fact label="buffer beyond the basin outline" id={data.grid.buffer_km} />
        </span>
        <span className="text-[12px] text-ink-3">
          grid <span data-ident>{data.grid.grid_id}</span> · <span data-ident>EPSG:{data.grid.epsg}</span> ·
          generated <span data-chrome>{data.generated_at.slice(0, 16).replace("T", " ")}</span>
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
              <li key={c} className="max-w-[86ch] text-[11.5px] text-ink-3" data-source-text>
                {c}
              </li>
            ))}
          </ul>
        </details>
      ) : null}
    </div>
  );
}

function Fact({ label, id }: { label: string; id: string }) {
  return (
    <span className="whitespace-nowrap">
      <V id={id} className="text-ink-2" /> <span className="text-ink-3">{label}</span>
    </span>
  );
}

// ---------- totals ----------

function Totals({ data }: { data: Readiness }) {
  return (
    <div className="grid grid-cols-4 gap-3">
      <Stat id={data.totals.sources} label="sources" title="Layers in the source register." />
      <Stat
        id={data.totals.verified_features}
        label="verified live"
        title="Feature sources confirmed by a live call to the service."
      />
      <Stat
        id={data.totals.redistributable}
        label="redistributable"
        title="Sources whose licence allows redistribution."
      />
      <Stat
        id={data.totals.gaps}
        label="no public source"
        title="Datasets a reader would expect here that no public service publishes."
        tone="miss"
      />
    </div>
  );
}

function Stat({ id, label, title, tone }: { id: string; label: string; title: string; tone?: "miss" }) {
  return (
    <div className="glass rounded-2xl p-4" title={title}>
      <div className={cn("text-[30px] leading-none", tone === "miss" ? "text-st-miss" : "text-ink")}>
        <V id={id} />
      </div>
      <div className="mt-1.5 text-[11.5px] text-ink-3">{label}</div>
    </div>
  );
}

// ---------- coverage by feature ----------

function FeatureSection({ data }: { data: Readiness }) {
  const { geological, effort } = useMemo(() => {
    const byCoverage = (a: ProspectFeature, b: ProspectFeature) =>
      numberOf(b.coverage) - numberOf(a.coverage);
    return {
      geological: data.features.filter((f) => !f.is_effort).sort(byCoverage),
      effort: data.features.filter((f) => f.is_effort).sort(byCoverage),
    };
  }, [data.features]);

  return (
    <section className="glass rounded-2xl p-4">
      <h2 className="text-[13px] text-ink">Coverage by feature</h2>
      <p className="mt-0.5 max-w-[86ch] text-[11.5px] text-ink-3">
        Share of grid cells with a real observation. No observation is a gap, not a low value.
      </p>

      <h3 className="mt-4 text-[12px] text-ink-2">
        Geological features (<V id={data.totals.geo_features} unit={false} />)
      </h3>
      <p className="mt-0.5 mb-1 max-w-[86ch] text-[11.5px] text-ink-3">
        Measured or mapped properties of the ground. Rows marked thin cover a minority of the basin.
      </p>
      <FeatureTable rows={geological} />

      <h3 className="mt-6 text-[12px] text-ink-2">
        Exploration-effort features (<V id={data.totals.effort_features} unit={false} />)
      </h3>
      <p className="mt-0.5 max-w-[86ch] text-[11.5px] text-ink-3">
        Where people looked, not what is in the rock: surveys flown, samples taken, holes collared.
      </p>
      <details className="mt-1 mb-1">
        <summary className="cursor-pointer text-[11px] text-ink-3 hover:text-ink-2">
          why these are kept apart
        </summary>
        <p className="mt-1 max-w-[86ch] border-line border-l pl-3 text-[11.5px] text-ink-3">
          A model trained on effort alone is the null model any later score has to beat. Beating it is the
          only way to show a score carries geology rather than exploration history.
        </p>
      </details>
      <FeatureTable rows={effort} />
    </section>
  );
}

function FeatureTable({ rows }: { rows: ProspectFeature[] }) {
  return (
    <table className="w-full text-[12.5px]">
      <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
        <tr>
          <th className="py-1.5 text-left font-normal">Feature</th>
          <th className="w-[170px] py-1.5 text-left font-normal">Coverage of the grid</th>
          <th className="w-[80px] py-1.5 text-right font-normal">Share</th>
          <th className="w-[110px] py-1.5 text-right font-normal">Cells covered</th>
          <th className="w-[130px] py-1.5 text-right font-normal">Observations per value</th>
        </tr>
      </thead>
      <tbody>
        {rows.map((f) => (
          <FeatureRow key={f.feature_key} feature={f} />
        ))}
      </tbody>
    </table>
  );
}

function FeatureRow({ feature: f }: { feature: ProspectFeature }) {
  return (
    <tr
      className="border-line border-t align-top"
      data-testid="feature-row"
      data-thin={f.thin ? "" : undefined}
    >
      <td className="py-2.5 pr-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className={cn("text-[13px]", f.thin ? "text-ink-2" : "text-ink")} data-source-text>
            {f.title}
          </span>
          {f.thin ? (
            <Chip tone="flag" className="uppercase tracking-wider">
              thin
            </Chip>
          ) : null}
        </div>
        {f.notes ? (
          <details className="mt-0.5">
            <summary className="cursor-pointer text-[11px] text-ink-3 hover:text-ink-2">
              <Meta feature={f} /> · note
            </summary>
            <p
              className="mt-1 max-w-[72ch] border-line border-l pl-3 text-[11.5px] text-ink-3"
              data-source-text
            >
              {f.notes}
            </p>
          </details>
        ) : (
          <div className="mt-0.5 text-[11px] text-ink-3">
            <Meta feature={f} />
          </div>
        )}
      </td>
      <td className="py-3 pr-4">
        <Bar share={numberOf(f.coverage)} thin={f.thin} />
      </td>
      <td className="py-2.5 text-right">
        <V id={f.coverage} className={f.thin ? "text-ink-3" : "text-ink"} />
      </td>
      <td className="py-2.5 text-right text-ink-2">
        <V id={f.covered_cells} />
      </td>
      <td className="py-2.5 text-right text-ink-2">
        <V id={f.median_obs} emptyText="not recorded" />
      </td>
    </tr>
  );
}

/** The identifiers under a feature's title: what it bears on, and the key it is stored under. */
function Meta({ feature: f }: { feature: ProspectFeature }) {
  return (
    <>
      bears on <span data-ident>{f.bears_on}</span> · <span data-ident>{f.feature_key}</span>
    </>
  );
}

function Bar({ share, thin }: { share: number; thin: boolean }) {
  const pct = Math.max(0, Math.min(1, share)) * 100;
  return (
    <div className="h-2 w-full overflow-hidden rounded-full bg-white/[0.06]" aria-hidden="true">
      <div
        className={cn("h-full rounded-full", thin ? "bg-ink-3/45" : "bg-ink-2")}
        style={{ width: `${pct}%` }}
      />
    </div>
  );
}

// ---------- sources ----------

const ROLE_NOTE: Record<ProspectSource["role"], string | null> = {
  feature: null,
  label:
    "A label, never used as a feature: deposits sit where people looked, so feeding them back would teach exploration history.",
  context: "Context only, not built into a feature.",
};

function SourcesSection({ sources }: { sources: ProspectSource[] }) {
  return (
    <Section
      title="Sources"
      hint="Every layer behind the features: licence, tier, and whether a live call confirmed it."
    >
      <table className="w-full text-[12.5px]">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="py-1.5 text-left font-normal">Source</th>
            <th className="w-[80px] py-1.5 text-left font-normal">Role</th>
            <th className="w-[70px] py-1.5 text-left font-normal">Tier</th>
            <th className="w-[240px] py-1.5 text-left font-normal">Licence</th>
            <th className="w-[100px] py-1.5 text-right font-normal">Records</th>
            <th className="w-[110px] py-1.5 text-right font-normal">Checked</th>
          </tr>
        </thead>
        <tbody>
          {sources.map((s) => (
            <SourceRow key={s.key} source={s} />
          ))}
        </tbody>
      </table>
    </Section>
  );
}

function SourceRow({ source: s }: { source: ProspectSource }) {
  const note = ROLE_NOTE[s.role];
  return (
    <tr
      className={cn("border-line border-t align-top", !s.verified && "opacity-60")}
      data-testid="source-row"
    >
      <td className="py-2.5 pr-4">
        <div className="flex flex-wrap items-center gap-2">
          <span className="text-[13px] text-ink" data-source-text>
            {s.title}
          </span>
          <a
            href={s.url}
            target="_blank"
            rel="noreferrer"
            className="text-ink-3 hover:text-ink-2"
            aria-label={`Open the ${s.title} service`}
          >
            <ArrowUpRight className="size-3.5" />
          </a>
          <span
            className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10.5px] text-ink-3"
            data-ident
          >
            {s.key}
          </span>
        </div>
        {note ? <p className="mt-1 max-w-[72ch] text-[11.5px] text-ink-3">{note}</p> : null}
        {s.notes || s.caveats.length ? (
          <details className="mt-1">
            <summary className="cursor-pointer text-[11px] text-ink-3 hover:text-ink-2">
              {s.caveats.length ? "note and what to watch" : "note"}
            </summary>
            <ul className="mt-1 space-y-1 border-line border-l pl-3">
              {[...(s.notes ? [s.notes] : []), ...s.caveats].map((c) => (
                <li key={c} className="max-w-[72ch] text-[11.5px] text-ink-3" data-source-text>
                  {c}
                </li>
              ))}
            </ul>
          </details>
        ) : null}
      </td>
      <td className="py-2.5 pr-3 text-ink-2">{s.role}</td>
      <td className="py-2.5 pr-3 text-ink-3">{s.tier}</td>
      <td className="py-2.5 pr-3">
        <a
          href={s.licence_url}
          target="_blank"
          rel="noreferrer"
          className="text-ink-3 underline decoration-line-strong underline-offset-4 hover:text-ink-2"
          data-source-text
        >
          {s.licence}
        </a>
        {!s.redistributable ? (
          <span className="mt-1 block text-[11px] text-ink-3">not redistributable</span>
        ) : null}
      </td>
      <td className="py-2.5 text-right text-ink-2">
        {s.record_count === null ? <span className="text-ink-3">—</span> : <V id={recordCountId(s.key)} />}
      </td>
      <td className="py-2.5 text-right">
        {s.verified ? (
          <span className="inline-flex items-center gap-1 text-[11.5px] text-ink-3">
            <Check className="size-3" aria-hidden="true" /> verified
          </span>
        ) : (
          <span className="inline-flex items-center gap-1 text-[11.5px] text-st-flag">
            <X className="size-3" aria-hidden="true" /> not verified
          </span>
        )}
      </td>
    </tr>
  );
}

// ---------- gaps ----------

const GAP_STATUS: Record<ProspectGap["status"], { label: string; tone: "miss" | "flag" }> = {
  not_addressable: { label: "not addressable", tone: "miss" },
  not_published: { label: "not published", tone: "miss" },
  not_public: { label: "not public", tone: "miss" },
  unverified: { label: "unverified", tone: "flag" },
  published_not_pulled: { label: "published, not yet pulled", tone: "flag" },
};

// ---------- the readiness gate ----------

const GATE_COLUMNS = ["present", "licensed", "covers", "servable", "versioned"] as const;
const GATE_KIND_LABEL: Record<ReadinessGate["rows"][number]["kind"], string> = {
  layer: "layer",
  scene: "scenes",
  label: "label",
  file: "file",
};

/**
 * The five columns every dataset must be green on before an agent phase starts. The notes are the gate's
 * own status strings, so they are shown as chrome; the counts they mention are values in the tables above.
 */
function GateSection({ gate }: { gate?: ReadinessGate }) {
  if (!gate) return null;
  const failing = gate.rows.filter((r) => GATE_COLUMNS.some((c) => !r[c].ok)).length;
  return (
    <Section
      title="The readiness gate"
      hint="Present in the store, licensed for how it is used, coverage stated, servable within its licence, and versioned to a hashed pull. No agent phase starts until every row is green."
    >
      <div className="mb-2 flex flex-wrap items-center gap-2 text-[12px]" data-testid="gate-verdict">
        <Chip tone={gate.green ? "pass" : "miss"}>{gate.green ? "gate green" : "gate red"}</Chip>
        <span className="text-ink-3" data-chrome>
          {failing} of {gate.rows.length} rows failing · store{" "}
          <span data-ident>{gate.store_sha256?.slice(0, 12) ?? "none"}</span>
          {gate.snapshot ? " (a named snapshot)" : " (no snapshot names it)"}
        </span>
      </div>
      <div className="overflow-x-auto">
        <table className="w-full text-[12px]" data-testid="gate-table">
          <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
            <tr>
              <th className="py-1 text-left font-normal">Dataset</th>
              <th className="py-1 text-left font-normal">Kind</th>
              {GATE_COLUMNS.map((c) => (
                <th key={c} className="py-1 text-left font-normal">
                  {c}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {gate.rows.map((r) => (
              <tr
                key={r.dataset}
                className="border-line border-t align-top"
                data-testid="gate-row"
                data-kind={r.kind}
              >
                <td className="py-1.5 pr-2">
                  <span data-ident>{r.dataset}</span>
                </td>
                <td className="py-1.5 pr-2 text-ink-3">{GATE_KIND_LABEL[r.kind]}</td>
                {GATE_COLUMNS.map((c) => (
                  <td key={c} className="py-1.5 pr-2" title={r[c].note}>
                    <span className={cn("block", r[c].ok ? "text-st-ok" : "text-st-miss")}>
                      {r[c].ok ? "ok" : "not yet"}
                    </span>
                    <span className="block max-w-[26ch] truncate text-[10.5px] text-ink-3" data-chrome>
                      {r[c].note}
                    </span>
                  </td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Section>
  );
}

function GapsSection({ gaps, totalId }: { gaps: ProspectGap[]; totalId: string }) {
  return (
    <section className="glass rounded-2xl p-4">
      <h2 className="text-[13px] text-ink">What is missing</h2>
      <p className="mt-0.5 mb-3 max-w-[86ch] text-[11.5px] text-ink-3">
        <V id={totalId} unit={false} /> datasets a reader would expect here that no public service publishes,
        and that no amount of work on public data will produce.
      </p>
      <div className="grid grid-cols-2 gap-3">
        {gaps.map((g) => {
          const status = GAP_STATUS[g.status];
          return (
            <article key={g.key} className="rounded-xl border border-line bg-black/20 p-3" data-testid="gap">
              <header className="flex flex-wrap items-center gap-2">
                <h3 className="text-[13px] text-ink" data-source-text>
                  {g.title}
                </h3>
                <Chip tone={status.tone}>{status.label}</Chip>
                <span
                  className="ml-auto rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10.5px] text-ink-3"
                  data-ident
                >
                  {g.key}
                </span>
              </header>
              <GapLine label="Why" text={g.why_it_matters} />
              <GapLine label="Workaround" text={g.workaround} />
              <details className="mt-1.5">
                <summary className="cursor-pointer text-[11px] text-ink-3 hover:text-ink-2">evidence</summary>
                <p className="mt-1 border-line border-l pl-3 text-[11.5px] text-ink-3" data-source-text>
                  {g.evidence}
                </p>
              </details>
            </article>
          );
        })}
      </div>
    </section>
  );
}

function GapLine({ label, text }: { label: string; text: string }) {
  return (
    <p className="mt-1.5 text-[11.5px] text-ink-2">
      <span className="text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">{label} </span>
      <span data-source-text>{text}</span>
    </p>
  );
}

// ---------- shared ----------

function Section({ title, hint, children }: { title: string; hint?: string; children: React.ReactNode }) {
  return (
    <section className="glass rounded-2xl p-4">
      <h2 className="text-[13px] text-ink">{title}</h2>
      {hint ? (
        <p className="mt-0.5 mb-2 max-w-[86ch] text-[11.5px] text-ink-3">{hint}</p>
      ) : (
        <div className="mb-2" />
      )}
      {children}
    </section>
  );
}
