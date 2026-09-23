import { ChevronRight, FileText, Search } from "lucide-react";
import { useState } from "react";
import { useReportIndex } from "@/features/report/useReport";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";

/**
 * The reports read, as a dropdown: closed it is one line with the count and the open report, so the layers
 * stay on screen; open it lists every file, with a filter over file number, company and era.
 */
export function ReportsList() {
  const { data } = useReportIndex();
  const open = useStore((s) => s.report);
  const openReport = useStore((s) => s.openReport);
  const [expanded, setExpanded] = useState(false);
  const [query, setQuery] = useState("");
  const reports = data?.reports ?? [];
  const q = query.trim().toLowerCase();
  const shown = q
    ? reports.filter((r) => `${r.file_num} ${r.company} ${r.era}`.toLowerCase().includes(q))
    : reports;
  return (
    <section className="mb-1" data-testid="reports-list">
      <button
        type="button"
        onClick={() => setExpanded((v) => !v)}
        aria-expanded={expanded}
        className="flex w-full items-center gap-1.5 rounded-lg px-2 pt-2 pb-1 text-left text-[10.5px] text-ink-3 uppercase tracking-[0.12em] hover:text-ink-2"
        data-testid="reports-toggle"
      >
        <ChevronRight className={cn("size-3 transition-transform", expanded && "rotate-90")} />
        Reports read
        <span className="text-ink-2 normal-case tracking-normal" data-instrument>
          {reports.length}
        </span>
        {!expanded && open ? (
          <span className="ml-auto truncate text-ink-2 normal-case tracking-normal" data-ident>
            {open}
          </span>
        ) : null}
      </button>
      {!expanded ? null : reports.length === 0 ? (
        <div className="mx-2 rounded-lg border border-line border-dashed px-2.5 py-2 text-[11.5px] text-ink-3">
          None read yet.
        </div>
      ) : (
        <>
          <label className="mx-2 mb-1 flex items-center gap-2 rounded-lg bg-black/25 px-2 py-1">
            <Search className="size-3 text-ink-3" aria-hidden="true" />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder="Filter"
              aria-label="Filter reports"
              className="min-w-0 flex-1 bg-transparent text-[12px] text-ink outline-none placeholder:text-ink-3"
            />
          </label>
          <div className="max-h-[260px] overflow-y-auto">
            {shown.map((r) => (
              <button
                key={r.file_num}
                type="button"
                onClick={() => openReport(r.file_num)}
                title={[r.company, r.era].filter(Boolean).join(" · ")}
                className={cn(
                  "flex w-full items-center gap-2 rounded-lg px-2 py-1 text-left transition-colors hover:bg-white/[0.04]",
                  open === r.file_num && "bg-white/[0.06]",
                )}
              >
                <FileText className="size-3.5 shrink-0 text-ink-3" />
                <span className="shrink-0 text-[12.5px] text-ink" data-ident>
                  {r.file_num}
                </span>
                <span className="min-w-0 flex-1 truncate text-[11px] text-ink-3" data-ident>
                  {[r.company, r.era].filter(Boolean).join(" · ")}
                </span>
              </button>
            ))}
            {shown.length === 0 ? <div className="px-2 py-1 text-[11.5px] text-ink-3">No match.</div> : null}
          </div>
        </>
      )}
    </section>
  );
}
