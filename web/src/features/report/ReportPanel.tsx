import { ChevronLeft, ChevronRight, ExternalLink, FileText, MapPinOff, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { Chip, StatusMark } from "@/components/ui/StatusMark";
import { V } from "@/components/values/V";
import type { ReportSummary } from "@/data/contract";
import { HolePanel } from "@/features/hole/HolePanel";
import { useStore } from "@/state/store";
import { useReportIndex } from "./useReport";

const POSITION_TEXT: Record<string, string> = {
  extracted_transformed: "Placed from printed coordinates",
  extracted_nad83: "Placed from printed NAD83 coordinates",
  provincial_geods: "Provincial position (GeoDS)",
  provincial_compilation: "Provincial position (compilation)",
  none: "Not placed",
};

export function ReportPanel() {
  const report = useStore((s) => s.report);
  const hole = useStore((s) => s.hole);
  const index = useReportIndex();
  const summary = index.data?.reports.find((r) => r.file_num === report) ?? null;

  return (
    <AnimatePresence>
      {report ? (
        <motion.aside
          key="report-panel"
          initial={{ opacity: 0, x: 24 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 24 }}
          transition={{ type: "spring", stiffness: 380, damping: 34 }}
          className="glass-opaque pointer-events-auto flex max-h-[calc(100vh-120px)] w-[440px] flex-col overflow-hidden rounded-2xl shadow-2xl shadow-black/50"
          data-strict="report-panel"
          aria-label="Report"
        >
          {summary ? (
            <Header summary={summary} hole={hole} />
          ) : (
            <div className="p-4 text-[13px] text-ink-3">Loading report…</div>
          )}
          <div className="min-h-0 flex-1 overflow-y-auto">
            {summary ? (
              hole ? (
                <HolePanel file={summary.file_num} holeId={hole} />
              ) : (
                <HoleList summary={summary} />
              )
            ) : null}
          </div>
        </motion.aside>
      ) : null}
    </AnimatePresence>
  );
}

function Header({ summary, hole }: { summary: ReportSummary; hole: string | null }) {
  const openReport = useStore((s) => s.openReport);
  const openHole = useStore((s) => s.openHole);
  return (
    <header className="border-line border-b px-4 pt-3.5 pb-3">
      <nav className="mb-2 flex items-center gap-1 text-[11.5px] text-ink-3" aria-label="Breadcrumb">
        <button type="button" className="hover:text-ink-2" onClick={() => openReport(null)}>
          Reports
        </button>
        <ChevronRight className="size-3" />
        <button
          type="button"
          className="tabular hover:text-ink-2"
          onClick={() => openHole(summary.file_num, null)}
          data-ident
        >
          {summary.file_num}
        </button>
        {hole ? (
          <>
            <ChevronRight className="size-3" />
            <span className="tabular text-ink-2" data-ident>
              {hole}
            </span>
          </>
        ) : null}
        <button
          type="button"
          className="ml-auto rounded-md p-1 hover:bg-white/5 hover:text-ink"
          onClick={() => openReport(null)}
          aria-label="Close report"
        >
          <X className="size-4" />
        </button>
      </nav>
      <div className="flex items-start gap-3">
        <span className="mt-0.5 flex size-9 shrink-0 items-center justify-center rounded-xl bg-white/[0.05]">
          <FileText className="size-4.5 text-ink-2" />
        </span>
        <div className="min-w-0 flex-1">
          <h2 className="truncate font-semibold text-[16px] text-ink tracking-tight" data-ident>
            Assessment file {summary.file_num}
          </h2>
          <div className="truncate text-[12.5px] text-ink-2">{summary.company}</div>
          <div className="mt-2 flex flex-wrap items-center gap-1.5">
            <Chip ident>{summary.era}</Chip>
            <Chip tone={summary.split === "heldout" ? "flag" : "neutral"}>
              {summary.split === "heldout" ? "held-out" : "development"}
            </Chip>
            <Chip>
              {summary.scan_kind === "scanned"
                ? "scanned"
                : summary.scan_kind === "text"
                  ? "born-digital"
                  : "mixed"}
            </Chip>
            <span className="text-[11.5px] text-ink-3">
              <V id={summary.year} /> · <V id={summary.page_count} /> pages
            </span>
          </div>
        </div>
      </div>
      <div className="mt-3 flex items-center gap-3 text-[12px]">
        <span className="flex items-center gap-1.5 text-ink-2">
          <StatusMark status="pass" size={11} /> <V id={summary.status_counts.pass} /> pass
        </span>
        <span className="flex items-center gap-1.5 text-ink-2">
          <StatusMark status="flag" size={11} /> <V id={summary.status_counts.flag} /> flagged
        </span>
        <span className="flex items-center gap-1.5 text-ink-2">
          <StatusMark status="miss" size={11} /> <V id={summary.status_counts.miss} /> missed
        </span>
        <a
          href={summary.source_url}
          target="_blank"
          rel="noreferrer"
          className="ml-auto flex items-center gap-1 text-ink-3 hover:text-ink-2"
        >
          Province <ExternalLink className="size-3" />
        </a>
      </div>
    </header>
  );
}

function HoleList({ summary }: { summary: ReportSummary }) {
  const openHole = useStore((s) => s.openHole);
  return (
    <div className="p-2">
      <div className="px-2 pt-2 pb-1 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">
        Holes read from this file
      </div>
      <ul>
        {summary.holes.map((h) => {
          return (
            <li key={h.hole_id}>
              <button
                type="button"
                onClick={() => openHole(summary.file_num, h.hole_id)}
                className="group flex w-full items-center gap-3 rounded-xl px-2.5 py-2.5 text-left transition-colors hover:bg-white/[0.04]"
              >
                <StatusMark status={h.status} />
                <span className="min-w-0 flex-1">
                  <span className="block truncate text-[13.5px] text-ink" data-ident>
                    {h.name}
                  </span>
                  <span className="flex items-center gap-1 truncate text-[11.5px] text-ink-3">
                    {h.lonlat ? null : <MapPinOff className="size-3" />}
                    {POSITION_TEXT[h.position_source]}
                    {h.datum_basis === "local_grid"
                      ? " · local grid"
                      : h.datum_basis === "none"
                        ? " · no datum"
                        : ""}
                  </span>
                </span>
                <ChevronLeft className="size-4 rotate-180 text-ink-3 opacity-0 transition-opacity group-hover:opacity-100" />
              </button>
            </li>
          );
        })}
      </ul>
    </div>
  );
}
