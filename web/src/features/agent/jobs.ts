import { useQuery, useQueryClient } from "@tanstack/react-query";
import { useEffect, useRef } from "react";
import { api } from "@/api/client";
import { CellJobs, Job, type ValRegistry, type ValueId } from "@/data/contract";
import { registerValues } from "@/data/registry";
import { keys } from "@/features/prospect/queries";
import { got, parsed } from "@/features/prospect/service";

/**
 * Background jobs on one cell: the service runs anything over a second on its own pool and keeps a
 * row per job, so the rail polls the row rather than holding a request open. The first kind is the staged
 * analyst; when one of its jobs finishes, the evidence record's chains are stale, and the record is
 * invalidated so the new chain appears where the stored ones do. The strip shows status, never merit.
 */

const READ_TIMEOUT_MS = 15_000;
/** How often a running job's row is re-read; nothing is polled once every job on the cell is final. */
export const POLL_MS = 1500;
export const ACTIVE = new Set<Job["status"]>(["queued", "running"]);

export const jobKeys = {
  cell: (cellId: string) => ["service", "jobs", cellId] as const,
};

export async function cellJobs(cellId: string): Promise<Job[]> {
  const raw = got(
    `/api/cell/${cellId}/jobs`,
    await api.GET("/api/cell/{cell_id}/jobs", {
      params: { path: { cell_id: cellId } },
      signal: AbortSignal.timeout(READ_TIMEOUT_MS),
    }),
  );
  const jobs = parsed(`/api/cell/${cellId}/jobs`, CellJobs, raw).jobs;
  registerJobCosts(jobs);
  return jobs;
}

export async function submitJob(body: {
  kind: string;
  cell_id: string;
  args?: Record<string, unknown>;
}): Promise<Job> {
  const raw = got("/api/jobs", await api.POST("/api/jobs", { body }));
  return parsed("/api/jobs", Job, raw);
}

export async function cancelJob(jobId: string): Promise<Job> {
  const raw = got(
    `/api/jobs/${jobId}/cancel`,
    await api.POST("/api/jobs/{job_id}/cancel", { params: { path: { job_id: jobId } } }),
  );
  return parsed(`/api/jobs/${jobId}/cancel`, Job, raw);
}

export function isActive(job: Job): boolean {
  return ACTIVE.has(job.status);
}

/** The value id a job's cost prints under: the same id the MCP `job_status` tool mints for it. */
export function jobCostId(jobId: string): ValueId {
  return `c:job:${jobId}:cost_usd` as ValueId;
}

/** A finished job's cost is a number the service reported; registered so the strip prints it like any other. */
export function registerJobCosts(jobs: Job[]): void {
  const reg: ValRegistry = {};
  for (const job of jobs) {
    const cost = job.result?.cost_usd;
    if (typeof cost !== "number") continue;
    reg[jobCostId(job.job_id)] = {
      id: jobCostId(job.job_id),
      kind: "stat",
      as_printed: null,
      value: cost,
      unit_as_printed: null,
      fmt: "m2",
      unit: "USD",
      note: `what ${job.kind} job ${job.job_id} spent on live model calls, as the service reported it`,
    };
  }
  if (Object.keys(reg).length) registerValues(reg, { notify: false });
}

/** The last stage the job reported, as a word: `stage:verify` reads as "verify"; before any stage, the status. */
export function latestStage(job: Job): string {
  for (let i = job.progress.length - 1; i >= 0; i--) {
    const event = job.progress[i]?.event ?? "";
    if (event.startsWith("stage:")) return event.slice("stage:".length);
  }
  return job.status;
}

/**
 * The analyst jobs that were queued or running at the last look and are done now: exactly the ones whose
 * chain the evidence record does not show yet. A job that was already done when the strip first looked has
 * nothing new to show, so it is not one of them.
 */
export function finishedAnalystJobs(previous: Job[] | undefined, next: Job[]): Job[] {
  if (!previous) return [];
  const before = new Map(previous.map((j) => [j.job_id, j.status]));
  return next.filter(
    (j) =>
      j.kind === "analyst" && j.status === "done" && before.has(j.job_id) && before.get(j.job_id) !== "done",
  );
}

export function useCellJobs(cellId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: jobKeys.cell(cellId ?? ""),
    queryFn: () => cellJobs(cellId as string),
    enabled: enabled && !!cellId,
    // polled only while something is queued or running; a final row does not change
    refetchInterval: (q) => (q.state.data?.some(isActive) ? POLL_MS : false),
  });
}

/** Watches a cell's jobs: an analyst job finishing invalidates the evidence record, so its chain appears. */
export function useChainRefresh(cellId: string | null, jobs: Job[] | undefined): void {
  const queryClient = useQueryClient();
  const previous = useRef<{ cellId: string | null; jobs: Job[] | undefined }>({
    cellId: null,
    jobs: undefined,
  });
  useEffect(() => {
    const last = previous.current;
    previous.current = { cellId, jobs };
    if (!cellId || !jobs || last.cellId !== cellId) return;
    if (finishedAnalystJobs(last.jobs, jobs).length) {
      void queryClient.invalidateQueries({ queryKey: keys.evidence(cellId) });
    }
  }, [cellId, jobs, queryClient]);
}
