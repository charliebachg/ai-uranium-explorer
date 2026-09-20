import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { api } from "@/api/client";
import { CellJobs, Job } from "@/data/contract";
import { registerValues } from "@/data/registry";
import { JobsStrip } from "@/features/agent/JobsStrip";
import * as jobs from "@/features/agent/jobs";

/**
 * The jobs strip's contract: the row shape the service serves (`agent.job`), what the strip prints for each
 * status, and the rule that decides when a finished analyst job means the evidence record must be re-read.
 * The service is faked at the module boundary; nothing here talks to a process.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CELL = "0201_0072";

function job(over: Partial<Job> = {}): Job {
  return {
    job_id: "a1b2c3d4e5f60718",
    kind: "analyst",
    cell_id: CELL,
    status: "running",
    requested_by: "local",
    args: { arm: "v1-openrouter", budget_usd: 0.5, reason: "", expert_ids: [] },
    created_at: "2026-09-21T10:15:00+00:00",
    started_at: "2026-09-21T10:15:01+00:00",
    finished_at: null,
    progress: [
      { at: "2026-09-21T10:15:00+00:00", event: "queued" },
      { at: "2026-09-21T10:15:01+00:00", event: "started" },
      { at: "2026-09-21T10:15:02+00:00", event: "stage:plan", n_segments: 10 },
      { at: "2026-09-21T10:15:20+00:00", event: "stage:execute", round: 0 },
    ],
    result: null,
    error: null,
    run_id: "20260921T101501Z-chain",
    ...over,
  };
}

const done = job({
  job_id: "ffffffff00000000",
  status: "done",
  finished_at: "2026-09-21T10:17:00+00:00",
  result: {
    chain_id: "20260921T101501Z-chain:0201_0072",
    verdict: "supports_closer_look",
    published: true,
    cost_usd: 0.12,
  },
});

describe("the job row contract", () => {
  it("parses the service's row as agent.job holds it, with its progress and result", () => {
    const parsed = CellJobs.parse({ cell_id: CELL, jobs: [job(), done] });
    expect(parsed.jobs[0]?.status).toBe("running");
    expect(parsed.jobs[1]?.result?.chain_id).toBe("20260921T101501Z-chain:0201_0072");
    expect(() => Job.parse(job({ status: "sleeping" as Job["status"] }))).toThrow();
  });

  it("names the last stage reached, and the status before any stage", () => {
    expect(jobs.latestStage(job())).toBe("execute");
    expect(jobs.latestStage(job({ progress: [{ at: "t", event: "queued" }] }))).toBe("running");
  });

  it("registers a finished job's cost under the same id the MCP tool mints for it", () => {
    jobs.registerJobCosts([done, job()]);
    expect(jobs.jobCostId(done.job_id)).toBe("c:job:ffffffff00000000:cost_usd");
  });
});

describe("when a finished analyst job refreshes the evidence record", () => {
  it("is exactly the jobs that were queued or running at the last look and are done now", () => {
    const running = job();
    const finished = { ...running, status: "done" as const, result: done.result };
    expect(jobs.finishedAnalystJobs(undefined, [finished])).toEqual([]);
    expect(jobs.finishedAnalystJobs([running], [finished])).toEqual([finished]);
    expect(jobs.finishedAnalystJobs([finished], [finished])).toEqual([]);
    expect(jobs.finishedAnalystJobs([], [finished])).toEqual([]);
    const other = { ...finished, kind: "corpus" };
    expect(jobs.finishedAnalystJobs([{ ...other, status: "running" }], [other])).toEqual([]);
    const failed = { ...running, status: "failed" as const, error: "process restarted" };
    expect(jobs.finishedAnalystJobs([running], [failed])).toEqual([]);
  });

  it("polls only while a job on the cell is queued or running", () => {
    expect(jobs.isActive(job())).toBe(true);
    expect(jobs.isActive(job({ status: "queued" }))).toBe(true);
    expect(jobs.isActive(done)).toBe(false);
    expect(jobs.isActive(job({ status: "cancelled" }))).toBe(false);
  });
});

describe("the strip", () => {
  let root: Root | null = null;
  let host: HTMLDivElement | null = null;

  afterEach(async () => {
    await act(async () => root?.unmount());
    host?.remove();
    root = null;
    host = null;
    vi.restoreAllMocks();
  });

  async function render(rows: Job[]) {
    // the typed client is the boundary: the strip's read of /api/cell/{id}/jobs answers from here
    vi.spyOn(api, "GET").mockResolvedValue({
      data: { cell_id: CELL, jobs: rows },
      error: undefined,
      response: new Response(null, { status: 200 }),
    } as never);
    registerValues({
      [jobs.jobCostId(done.job_id)]: {
        id: jobs.jobCostId(done.job_id),
        kind: "stat",
        as_printed: null,
        value: 0.12,
        unit_as_printed: null,
        fmt: "m2",
        unit: "USD",
      },
    });
    const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
    host = document.createElement("div");
    document.body.appendChild(host);
    root = createRoot(host);
    await act(async () => {
      root?.render(
        <QueryClientProvider client={client}>
          <JobsStrip cellId={CELL} state="up" />
        </QueryClientProvider>,
      );
    });
    await act(async () => {
      await new Promise((r) => setTimeout(r, 0));
    });
    return host;
  }

  it("prints each job as a status and its stage, and a finished one as its chain and verdict", async () => {
    const el = await render([
      job(),
      done,
      job({ job_id: "0000000000000001", status: "failed", error: "process restarted" }),
    ]);
    const rows = Array.from(el.querySelectorAll("[data-testid=job-row]"));
    expect(rows.map((r) => r.getAttribute("data-status"))).toEqual(["running", "done", "failed"]);
    expect(rows[0]?.querySelector("[data-testid=job-stage]")?.textContent).toBe("at execute");
    expect(rows[0]?.querySelector("[data-testid=cancel-job]")).not.toBeNull();
    expect(rows[1]?.textContent).toContain("20260921T101501Z-chain:0201_0072");
    expect(rows[1]?.textContent).toContain("supports closer look");
    expect(rows[1]?.querySelector("[data-vid]")?.textContent).toContain("0.12");
    expect(rows[1]?.querySelector("[data-testid=cancel-job]")).toBeNull();
    expect(rows[2]?.querySelector("[data-testid=job-error]")?.textContent).toBe("process restarted");
    const run = el.querySelector<HTMLButtonElement>("[data-testid=run-analyst]");
    expect(run?.disabled).toBe(true);
    // no colour carries merit: every row's status chip has the same classes
    const chips = rows.map((r) => r.querySelector("[data-testid=job-status]")?.className);
    expect(new Set(chips).size).toBe(1);
  });

  it("says when the cell has no jobs and lets the analyst be started", async () => {
    const el = await render([]);
    expect(el.querySelector("[data-testid=jobs-strip]")?.textContent).toContain("none for this cell");
    expect(el.querySelector<HTMLButtonElement>("[data-testid=run-analyst]")?.disabled).toBe(false);
  });
});
