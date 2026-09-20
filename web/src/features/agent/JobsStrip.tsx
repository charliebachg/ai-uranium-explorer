import { useMutation, useQueryClient } from "@tanstack/react-query";
import { Play, X } from "lucide-react";
import { useState } from "react";
import { V } from "@/components/values/V";
import type { Job } from "@/data/contract";
import { hasValue } from "@/data/registry";
import type { ServiceState } from "@/features/prospect/OfflineNotice";
import { cn } from "@/lib/cn";
import {
  cancelJob,
  isActive,
  jobCostId,
  jobKeys,
  latestStage,
  submitJob,
  useCellJobs,
  useChainRefresh,
} from "./jobs";

/**
 * The cell's background jobs, under the scores and above the tabs: what is queued or running, which stage it
 * reached, and what a finished one produced. A job is a status, not a merit: every row prints the same way,
 * and a verdict is a word beside a chain id, not a colour. Polled only while something is running; when an
 * analyst job finishes the evidence record is re-read so its chain appears in the panel below.
 */

export function JobsStrip({ cellId, state }: { cellId: string; state: ServiceState }) {
  const queryClient = useQueryClient();
  const jobsQ = useCellJobs(cellId, state === "up");
  const jobs = jobsQ.data ?? [];
  useChainRefresh(cellId, jobsQ.data);
  const [note, setNote] = useState<string | null>(null);

  const refresh = () => queryClient.invalidateQueries({ queryKey: jobKeys.cell(cellId) });
  const run = useMutation({
    mutationFn: () =>
      submitJob({ kind: "analyst", cell_id: cellId, args: { reason: "asked from the agent rail" } }),
    onMutate: () => setNote(null),
    onSuccess: () => void refresh(),
    onError: (err) => setNote(reason(err)),
  });
  const cancel = useMutation({
    mutationFn: (jobId: string) => cancelJob(jobId),
    onSuccess: () => void refresh(),
    onError: (err) => setNote(reason(err)),
  });

  if (state !== "up") return null;
  const busy = jobs.some(isActive);

  return (
    <section
      className="shrink-0 border-line border-b px-4 py-2 text-[11.5px]"
      aria-label="Background jobs"
      data-testid="jobs-strip"
    >
      <div className="flex items-center gap-2">
        <span className="text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">Jobs</span>
        {jobs.length === 0 ? <span className="text-ink-3">none for this cell</span> : null}
        <button
          type="button"
          onClick={() => run.mutate()}
          disabled={busy || run.isPending}
          className={cn(
            "ml-auto flex items-center gap-1 rounded-md bg-black/25 px-2 py-1 text-[11px] text-ink-3 transition-colors",
            "hover:text-ink-2 disabled:cursor-not-allowed disabled:opacity-50",
          )}
          title="Run the staged analyst on this cell as a background job (enabled cells only)"
          data-testid="run-analyst"
        >
          <Play className="size-3" aria-hidden="true" />
          Run analyst
        </button>
      </div>
      {note ? (
        <p className="mt-1 text-ink-2" data-testid="jobs-note">
          {note}
        </p>
      ) : null}
      {jobs.length ? (
        <ul className="mt-1.5 space-y-1">
          {jobs.map((job) => (
            <JobRow key={job.job_id} job={job} onCancel={() => cancel.mutate(job.job_id)} />
          ))}
        </ul>
      ) : null}
    </section>
  );
}

/** The server's reason, when it gave one (a 4xx detail), else the error's own words. */
function reason(err: unknown): string {
  const text = err instanceof Error ? err.message : String(err);
  const m = /\((.*)\)$/.exec(text);
  return m?.[1] ?? text;
}

function JobRow({ job, onCancel }: { job: Job; onCancel: () => void }) {
  const active = isActive(job);
  const result = job.result ?? {};
  const chainId = typeof result.chain_id === "string" ? result.chain_id : null;
  const verdict = typeof result.verdict === "string" ? result.verdict.replace(/_/g, " ") : null;
  const costId = jobCostId(job.job_id);
  return (
    <li
      className="flex flex-wrap items-baseline gap-x-2 gap-y-0.5"
      data-testid="job-row"
      data-status={job.status}
    >
      <span className="text-ink-2">{job.kind}</span>
      <span
        className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10.5px] text-ink"
        data-testid="job-status"
      >
        {job.status}
      </span>
      {active ? (
        <span className="text-ink-3" data-testid="job-stage">
          {latestStage(job) === job.status ? "waiting" : `at ${latestStage(job)}`}
        </span>
      ) : null}
      {job.status === "done" && chainId ? (
        <span className="text-ink-3">
          chain{" "}
          <span className="font-mono text-[10.5px] text-ink-2" data-ident>
            {chainId}
          </span>
          {verdict ? <span data-source-text> · {verdict}</span> : null}
          {hasValue(costId) ? (
            <>
              {" · "}
              <V id={costId} className="text-ink-2" />
            </>
          ) : null}
        </span>
      ) : null}
      {job.error ? (
        <span className="text-ink-3" data-testid="job-error">
          {job.error}
        </span>
      ) : null}
      <span className="ml-auto text-[10.5px] text-ink-3" data-chrome>
        {job.requested_by} · {job.created_at.slice(11, 16)}
      </span>
      {active ? (
        <button
          type="button"
          onClick={onCancel}
          className="rounded p-0.5 text-ink-3 hover:bg-white/5 hover:text-ink"
          aria-label={`Cancel job ${job.job_id}`}
          data-testid="cancel-job"
        >
          <X className="size-3" />
        </button>
      ) : null}
    </li>
  );
}
