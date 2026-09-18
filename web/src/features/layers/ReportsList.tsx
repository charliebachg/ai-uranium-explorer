import { FileText } from "lucide-react";
import { useReportIndex } from "@/features/report/useReport";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";

export function ReportsList() {
  const { data } = useReportIndex();
  const open = useStore((s) => s.report);
  const openReport = useStore((s) => s.openReport);
  const reports = data?.reports ?? [];
  return (
    <section className="mb-1">
      <div className="px-2 pt-2 pb-1 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">Reports read</div>
      {reports.length === 0 ? (
        <div className="mx-2 rounded-lg border border-line border-dashed px-2.5 py-2 text-[11.5px] text-ink-3">
          No reports have been read yet.
        </div>
      ) : (
        reports.map((r) => (
          <button
            key={r.file_num}
            type="button"
            onClick={() => openReport(r.file_num)}
            className={cn(
              "flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors hover:bg-white/[0.04]",
              open === r.file_num && "bg-white/[0.06]",
            )}
          >
            <span className="flex size-6 shrink-0 items-center justify-center rounded-md bg-black/30">
              <FileText className="size-3.5 text-ink-2" />
            </span>
            <span className="min-w-0 flex-1">
              <span className="block truncate text-[13px] text-ink" data-ident>
                {r.file_num}
              </span>
              <span className="block truncate text-[11px] text-ink-3">
                {r.company} · {r.era}
              </span>
            </span>
          </button>
        ))
      )}
    </section>
  );
}
