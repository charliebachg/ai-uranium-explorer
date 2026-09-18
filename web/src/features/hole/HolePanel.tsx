import { FileSearch, MapPinOff, Ruler } from "lucide-react";
import { useState } from "react";
import { Chip, StatusMark } from "@/components/ui/StatusMark";
import { V } from "@/components/values/V";
import type { Hole, Report } from "@/data/contract";
import { resolveValue } from "@/data/registry";
import { useReport } from "@/features/report/useReport";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";
import { StripLog } from "./StripLog";

/** Evidence for one hole: collar as printed, datum and placement, cross-checks, intervals and a strip log. */
export function HolePanel({ file, holeId }: { file: string; holeId: string }) {
  const { data: report, error } = useReport(file);
  if (error) return <div className="p-4 text-[13px] text-st-miss">{error}</div>;
  if (!report) return <div className="p-4 text-[13px] text-ink-3">Loading hole…</div>;
  const hole = report.holes.find((h) => h.hole_id === holeId);
  if (!hole) return <div className="p-4 text-[13px] text-ink-3">This hole is not in the report data.</div>;
  return <HoleBody report={report} hole={hole} />;
}

function useOpenValue(file: string) {
  const openValue = useStore((s) => s.openValue);
  return (id: string) => {
    const v = resolveValue(id);
    const page = v?.lineage?.page;
    openValue(id, page ? { file, page } : undefined);
  };
}

function HoleBody({ report, hole }: { report: Report; hole: Hole }) {
  const file = report.summary.file_num;
  const open = useOpenValue(file);
  const [tab, setTab] = useState<"assays" | "lith" | "prov">(
    hole.assays.length ? "assays" : hole.lith.length ? "lith" : "prov",
  );
  const c = hole.collar;

  const collarRows: { label: string; id: string | null }[] = [
    { label: "Easting", id: c.easting },
    { label: "Northing", id: c.northing },
    { label: "Latitude", id: c.lat },
    { label: "Longitude", id: c.lon },
    { label: "Local grid position", id: c.grid_x },
    { label: "UTM zone", id: c.utm_zone },
    { label: "Elevation", id: c.elevation },
    { label: "Azimuth", id: c.azimuth },
    { label: "Dip", id: c.dip },
    { label: "Total depth", id: c.total_depth },
  ].filter((r) => r.id || ["Elevation", "Dip", "Total depth"].includes(r.label));

  return (
    <div className="space-y-3 p-4">
      <div className="flex items-center gap-2.5">
        <StatusMark status={hole.status} size={14} />
        <h3 className="font-semibold text-[20px] text-ink tracking-tight">
          <V id={hole.name} onSelect={open} className="no-underline" />
        </h3>
        <Chip
          tone={hole.status === "pass" ? "pass" : hole.status === "flag" ? "flag" : "miss"}
          className="ml-auto"
        >
          {hole.status === "pass"
            ? "all checks passed"
            : hole.status === "flag"
              ? "flagged for review"
              : "values missed"}
        </Chip>
      </div>

      <Section title="Collar, as printed" icon={<FileSearch className="size-3.5" />}>
        <div className="grid grid-cols-2 gap-1.5">
          {collarRows.map((r) => (
            <ValueCell key={r.label} label={r.label} id={r.id} onOpen={open} />
          ))}
        </div>
      </Section>

      <Section title="Datum and placement" icon={<Ruler className="size-3.5" />}>
        <DatumCard hole={hole} onOpen={open} />
      </Section>

      <Section title="Intervals">
        <div className="mb-2 flex gap-1 rounded-lg bg-black/25 p-1 text-[12px]">
          {(
            [
              ["assays", "Assays and probe", hole.assays.length],
              ["lith", "Lithology", hole.lith.length],
              ["prov", "Provincial lithology", hole.provincial_lith.length],
            ] as const
          ).map(([k, label, n]) => (
            <button
              key={k}
              type="button"
              aria-pressed={tab === k}
              onClick={() => setTab(k)}
              disabled={n === 0}
              className={cn(
                "flex-1 rounded-md px-2 py-1.5 transition-colors",
                tab === k ? "bg-raised text-ink" : "text-ink-3 hover:text-ink-2",
                n === 0 && "cursor-not-allowed opacity-40",
              )}
            >
              {label}
            </button>
          ))}
        </div>
        {tab === "assays" ? <AssayTable hole={hole} onOpen={open} /> : null}
        {tab === "lith" ? <LithTable rows={hole.lith} onOpen={open} /> : null}
        {tab === "prov" ? <LithTable rows={hole.provincial_lith} onOpen={open} provincial /> : null}
        {hole.assays.length + hole.lith.length + hole.provincial_lith.length > 0 ? (
          <div className="mt-3">
            <StripLog hole={hole} onOpen={open} />
          </div>
        ) : null}
      </Section>
    </div>
  );
}

function Section({
  title,
  icon,
  children,
}: {
  title: string;
  icon?: React.ReactNode;
  children: React.ReactNode;
}) {
  return (
    <section className="rounded-xl border border-line bg-black/15 p-3">
      <div className="mb-2 flex items-center gap-1.5 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">
        {icon}
        {title}
      </div>
      {children}
    </section>
  );
}

function ValueCell({
  label,
  id,
  onOpen,
}: {
  label: string;
  id: string | null;
  onOpen: (id: string) => void;
}) {
  const v = id ? resolveValue(id) : null;
  const status = v?.status;
  return (
    <div
      className={cn("rounded-lg bg-white/[0.03] px-2.5 py-2", status === "flag" && "ring-1 ring-st-flag/30")}
    >
      <div className="flex items-center gap-1 text-[10.5px] text-ink-3">
        {label}
        {v?.lineage?.unit_source === "page_note" ? (
          <span title="unit taken from a page note, not the cell">· unit from page note</span>
        ) : null}
      </div>
      <div className="mt-0.5 text-[14px] text-ink">
        {id ? (
          <V id={id} onSelect={onOpen} />
        ) : (
          <span className="text-[12.5px] text-ink-3 italic">not printed</span>
        )}
      </div>
    </div>
  );
}

function DatumCard({ hole, onOpen }: { hole: Hole; onOpen: (id: string) => void }) {
  const c = hole.collar;
  const p = hole.position;
  return (
    <div className="space-y-2 text-[12.5px]">
      <div className="flex items-center justify-between gap-2">
        <span className="text-ink-3">Coordinates</span>
        <Chip tone={c.coord_kind === "local_grid" || c.coord_kind === "not_printed" ? "flag" : "neutral"}>
          {c.coord_kind === "utm"
            ? "UTM"
            : c.coord_kind === "geographic"
              ? "latitude and longitude"
              : c.coord_kind === "local_grid"
                ? "local grid"
                : "not printed"}
        </Chip>
      </div>
      <div className="flex items-center justify-between gap-2">
        <span className="text-ink-3">Datum on page</span>
        {c.datum_printed ? (
          <V id={c.datum_printed} onSelect={onOpen} />
        ) : (
          <span className="text-ink-2">not stated on page</span>
        )}
      </div>
      {p?.transform ? (
        <div className="rounded-lg bg-white/[0.03] p-2.5">
          <div className="text-ink-2">{p.transform.name}</div>
          <div className="mt-0.5 text-[11.5px] text-ink-3" data-ident>
            {p.transform.code} · {p.transform.grid_file ?? "no grid"}
          </div>
          <div className="mt-1 flex items-center justify-between">
            <span className="text-ink-3">Shift applied</span>
            <V id={p.transform.shift_m} />
          </div>
        </div>
      ) : null}
      <div className="flex items-start gap-2 rounded-lg bg-white/[0.03] p-2.5 text-ink-2">
        {p ? null : <MapPinOff className="mt-0.5 size-3.5 shrink-0 text-st-flag" />}
        <span>
          {p
            ? p.source === "extracted_transformed"
              ? "Placed on the map from the printed coordinates after the datum transformation above."
              : p.source === "extracted_nad83"
                ? "Placed on the map from printed NAD83 coordinates."
                : "Placed at the provincial position for this hole name. The report's own coordinates were not used."
            : c.coord_kind === "local_grid"
              ? "Not placed: the page gives a local grid position with no tie to UTM, and no provincial hole matches this name."
              : "Not placed: the page prints no coordinates for this hole and no provincial hole matches this name."}
        </span>
      </div>
      {hole.matches.map((m) => {
        const note = resolveValue(m.offset_m)?.note;
        return (
          <div key={`${m.dataset}:${m.feature_id}`} className="rounded-lg bg-white/[0.03] px-2.5 py-2">
            <div className="flex items-center justify-between gap-2">
              <Chip tone={m.dataset === "geods" ? "geods" : "compilation"}>
                {m.dataset === "geods" ? "GeoDS" : "compilation"}
              </Chip>
              <span className="text-ink-3">offset</span>
              <V id={m.offset_m} />
              {m.datum_shift_signature ? <Chip tone="flag">looks like a datum shift</Chip> : null}
            </div>
            {note ? (
              <div className="mt-1 text-[11.5px] text-st-flag/90" data-source-text>
                {note}
              </div>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}

function AssayTable({ hole, onOpen }: { hole: Hole; onOpen: (id: string) => void }) {
  const hover = useStore((s) => s.hoverInterval);
  const setHover = useStore((s) => s.setHoverInterval);
  if (!hole.assays.length) return <Empty>No assay or probe rows extracted for this hole.</Empty>;
  return (
    <div className="overflow-hidden rounded-lg border border-line">
      <table className="w-full text-[12.5px]">
        <thead className="bg-white/[0.03] text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="px-2 py-1.5 text-left font-normal">Sample</th>
            <th className="px-2 py-1.5 text-right font-normal">From</th>
            <th className="px-2 py-1.5 text-right font-normal">To</th>
            <th className="px-2 py-1.5 text-left font-normal">Value as printed</th>
            <th className="w-5 px-1" />
          </tr>
        </thead>
        <tbody>
          {hole.assays.map((a) => (
            <tr
              key={a.id}
              data-hot={hover === a.id ? "1" : undefined}
              onMouseEnter={() => setHover(a.id)}
              onMouseLeave={() => setHover(null)}
              className="border-line border-t transition-colors data-[hot=1]:bg-white/[0.05]"
            >
              <td className="px-2 py-1.5">
                {a.sample_id ? (
                  <V id={a.sample_id} onSelect={onOpen} />
                ) : (
                  <span className="text-ink-3">·</span>
                )}
              </td>
              <td className="px-2 py-1.5 text-right">
                <V id={a.from} onSelect={onOpen} />
              </td>
              <td className="px-2 py-1.5 text-right">
                {a.to !== a.from ? <V id={a.to} onSelect={onOpen} /> : <span className="text-ink-3">·</span>}
              </td>
              <td className="px-2 py-1.5">
                {a.grades.map((g) => (
                  <div key={g.value} className="flex flex-wrap items-center gap-1">
                    <V id={g.value} onSelect={onOpen} emptyText="not printed" />
                    {g.basis === "probe_equivalent" ? <Chip tone="flag">probe, not chemical</Chip> : null}
                    {g.species !== "not_printed" ? <Chip ident>{g.species}</Chip> : null}
                  </div>
                ))}
              </td>
              <td className="px-1">
                <StatusMark status={a.status} size={10} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function LithTable({
  rows,
  onOpen,
  provincial = false,
}: {
  rows: Hole["lith"];
  onOpen: (id: string) => void;
  provincial?: boolean;
}) {
  const hover = useStore((s) => s.hoverInterval);
  const setHover = useStore((s) => s.setHoverInterval);
  if (!rows.length)
    return (
      <Empty>
        {provincial ? "No provincial lithology for this hole." : "No lithology rows extracted for this hole."}
      </Empty>
    );
  return (
    <div className="overflow-hidden rounded-lg border border-line">
      <table className="w-full text-[12.5px]">
        <thead className="bg-white/[0.03] text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="px-2 py-1.5 text-right font-normal">From</th>
            <th className="px-2 py-1.5 text-right font-normal">To</th>
            <th className="px-2 py-1.5 text-left font-normal">
              {provincial ? "Provincial description" : "Description as printed"}
            </th>
          </tr>
        </thead>
        <tbody>
          {rows.map((l) => (
            <tr
              key={l.id}
              data-hot={hover === l.id ? "1" : undefined}
              onMouseEnter={() => setHover(l.id)}
              onMouseLeave={() => setHover(null)}
              className="border-line border-t data-[hot=1]:bg-white/[0.05]"
            >
              <td className="px-2 py-1.5 text-right">
                <V id={l.from} onSelect={provincial ? undefined : onOpen} />
              </td>
              <td className="px-2 py-1.5 text-right">
                <V id={l.to} onSelect={provincial ? undefined : onOpen} />
              </td>
              <td className="px-2 py-1.5">
                {l.description ? (
                  <V id={l.description} onSelect={provincial ? undefined : onOpen} />
                ) : l.code ? (
                  <V id={l.code} />
                ) : null}
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </div>
  );
}

function Empty({ children }: { children: React.ReactNode }) {
  return (
    <div className="rounded-lg border border-line border-dashed px-3 py-3 text-[12px] text-ink-3">
      {children}
    </div>
  );
}
