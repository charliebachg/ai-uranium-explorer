import { existsSync, readFileSync } from "node:fs";
import { resolve } from "node:path";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, describe, expect, it, vi } from "vitest";
import { RecordedChat, type ValueId } from "@/data/contract";
import { recordedCellTarget } from "@/features/tour/targets";
import { useStore } from "@/state/store";

/**
 * The recorded session the walkthrough replays (`ue prospect record`): its contract, what the tour reads off
 * it, and the chat panel drawing it exactly as it draws a live session, with nothing asked of the service.
 * `<V>` throws on an unbacked id under vitest, so a render that completes is itself the check that every
 * number on the cards resolves to a registered value.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CELL = "0201_0072";
const CONDUCTOR = `c:cell:${CELL}:d_conductor_m`;
const JOB = "9d1e7c0a5b3f4e21";
const CHAIN = `20260921T100000Z-chain:${CELL}`;

const serviceCalls: string[] = [];
vi.mock("@/features/prospect/service", async () => ({
  SERVICE_ROOT: "http://test",
  SERVICE_COMMAND: "ue prospect serve",
  health: async () => false,
  candidates: async () => [],
  evidence: async () => null,
  conversations: async () => ({ cell_id: CELL, conversations: [] }),
  conversation: async () => null,
  askStreaming: async () => {
    serviceCalls.push("ask");
    throw new Error("nothing is asked in a replay");
  },
  job: async () => {
    serviceCalls.push("job");
    throw new Error("nothing is polled in a replay");
  },
}));

vi.mock("@/data/loader", async (importOriginal) => {
  const mod = await importOriginal<typeof import("@/data/loader")>();
  const { registerValues } = await import("@/data/registry");
  return {
    ...mod,
    // as the real loader does: the cited values join the registry, so the chips resolve
    loadRecordedChat: async () => {
      const rec = recording();
      registerValues(rec.values as never);
      return rec;
    },
  };
});

const { ChatPanel } = await import("@/features/prospect/ChatPanel");

function stat(id: string, value: number, fmt: "ratio3" | "int" | "m1" | "m2", unit?: string) {
  return {
    id: id as ValueId,
    kind: "stat" as const,
    as_printed: null,
    value,
    unit_as_printed: null,
    fmt,
    unit,
  };
}

const ROUTE = {
  kind: "lookup",
  topic: "features",
  cell_ids: [CELL],
  entities: [],
  out_of_scope: false,
  detail: "",
  reason: "asks what was measured",
  fallback: null,
  meaning: "a value the store holds for this cell",
  plan: [{ tool: "cell_features", args: { cell_id: CELL } }],
};

function turn(over: Record<string, unknown>) {
  return {
    question: "q",
    text: null,
    claims: [],
    caveats: [],
    cannot_answer: false,
    published: true,
    problems: [],
    tools_used: [],
    cost_usd: 0.002,
    model: "z-ai/glm-5.3-flash",
    route: ROUTE,
    abstention: null,
    insight: null,
    job: null,
    jobs_done: [],
    expert_ids: [],
    retried: false,
    ...over,
  };
}

const RESULT = {
  chain_id: CHAIN,
  run_id: "20260921T100000Z-chain",
  arm: "v1-openrouter",
  verdict: "insufficient",
  probability: 0.8,
  published: false,
  problems: ["the verifier refused node n02 twice"],
  cost_usd: 0.041,
};
const PROGRESS = [
  { at: "2026-09-21T10:00:00+00:00", event: "queued" },
  { at: "2026-09-21T10:00:01+00:00", event: "started" },
  { at: "2026-09-21T10:00:02+00:00", event: "run", run_id: "20260921T100000Z-chain" },
  { at: "2026-09-21T10:00:20+00:00", event: "stage:execute", ms: 18000, n_segments: 5 },
  { at: "2026-09-21T10:01:10+00:00", event: "stage:verify", ms: 50000, round: 1 },
  { at: "2026-09-21T10:01:30+00:00", event: "stage:decide", ms: 20000, published: false },
  { at: "2026-09-21T10:01:31+00:00", event: "done" },
];
const HANDOVER = {
  job_id: JOB,
  cell_id: CELL,
  reason: "Run the analyst on this cell and tell me what it decides.",
  expert_ids: [],
  score_ids: [`c:score:${CELL}:learned`],
  budget_usd: 0.5,
  requested_by: "tour",
  submitted_at: "2026-09-21T10:00:00+00:00",
};
const DIFF = {
  chain_id: CHAIN,
  baseline_chain_id: `20260920T130034Z-chain:${CELL}`,
  verdict: { before: "supports_closer_look", after: "insufficient", changed: true },
  nodes: [
    {
      node_id: "n02",
      criterion: "fault_proximity",
      kind: "criterion",
      before: "met",
      after: "unknown",
      expert_ids: [],
      changed: true,
    },
    {
      node_id: "n01",
      criterion: "conductor_proximity",
      kind: "criterion",
      before: "met",
      after: "met",
      expert_ids: [],
      changed: false,
    },
  ],
  n_changed: 1,
  n_leaning_on_expert: 0,
  expert_ids: [],
  note: "node statuses and the verdict of the run with the insight, beside the stored run without it",
};

/** A recording shaped as `ue prospect record` writes one: four questions, the invocation, the follow-up. */
function recording() {
  return {
    cell_id: CELL,
    lon: -103.7255,
    lat: 58.1579,
    recorded_at: "2026-09-21T10:02:00+00:00",
    model: "z-ai/glm-5.3-flash",
    effort: "low",
    requested_by: "tour",
    cost_usd: 0.031,
    tools_available: ["cell_features", "cell_scores", "criteria_breakdown", "label_context"],
    turns: [
      turn({
        question: "What is actually measured in this cell, and what is only assumed?",
        text: `The nearest mapped conductor is 820.0 m away (${CONDUCTOR}).`,
        claims: [{ text: "820.0 m to the conductor", value_ids: [CONDUCTOR] }],
        tools_used: ["cell_features"],
      }),
      turn({
        question: "Why is the criteria score what it is?",
        text: null,
        published: false,
        problems: ["claim 2: the number 0.93 is not backed by any value this claim cites"],
        retried: true,
        route: { ...ROUTE, kind: "explain_score", plan: [{ tool: "cell_scores", args: { cell_id: CELL } }] },
      }),
      turn({
        question: "What grade would a hole drilled here intersect?",
        text: "No answer: this record never says that.",
        cannot_answer: true,
        route: { ...ROUTE, kind: "lookup", out_of_scope: true, detail: "asks for a grade", plan: [] },
        abstention: {
          abstain_id: "a:1f2e3d4c:1",
          reason: "out_of_scope",
          detail: "asks for a grade",
          said: "this record never says that",
          recorded_at: "2026-09-21T10:00:00+00:00",
        },
        tools_used: ["abstain"],
      }),
      turn({
        question: HANDOVER.reason,
        text: `Analyst invoked on cell ${CELL}.`,
        route: { ...ROUTE, kind: "run_analyst", plan: [] },
        // the row as it ended, where the live card would have polled it
        job: {
          ...HANDOVER,
          status: "done",
          progress: PROGRESS,
          result: RESULT,
          error: null,
          run_id: RESULT.run_id,
          started_at: "t",
          finished_at: "t",
        },
        tools_used: ["run_analyst"],
      }),
      turn({
        question: "The analyst has finished. What did it decide?",
        text: "The analyst has reported.",
        jobs_done: [
          {
            ...HANDOVER,
            status: "done",
            progress: PROGRESS,
            result: RESULT,
            verdict: "insufficient",
            assessment: DIFF,
            reported: true,
          },
        ],
      }),
    ],
    job: {
      job_id: JOB,
      cell_id: CELL,
      kind: "analyst",
      requested_by: "tour",
      created_at: "t",
      status: "done",
      progress: PROGRESS,
      result: RESULT,
      error: null,
      run_id: RESULT.run_id,
      started_at: "t",
      finished_at: "t",
    },
    values: { [CONDUCTOR]: stat(CONDUCTOR, 820, "m1", "m") },
  };
}

let root: Root | null = null;
let container: HTMLElement | null = null;

async function mount(turns: number): Promise<HTMLElement> {
  useStore.setState({ chatReplay: { cellId: CELL, turns } });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  await act(async () => {
    root?.render(
      <QueryClientProvider client={client}>
        <ChatPanel cellId={CELL} state="down" />
      </QueryClientProvider>,
    );
    await new Promise((r) => setTimeout(r, 0));
  });
  // the recording loads on the first effect; one more tick draws it
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
  return container;
}

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = null;
  container = null;
  serviceCalls.length = 0;
  useStore.setState({ chatReplay: null });
});

describe("the recorded session contract", () => {
  it("reads the shape `ue prospect record` writes: interface turns, the job as its row ended, the values cited", () => {
    const rec = RecordedChat.parse(recording());
    expect(rec.turns[3]?.route?.kind).toBe("run_analyst");
    expect(rec.turns[2]?.abstention?.reason).toBe("out_of_scope");
    expect(rec.job?.status).toBe("done");
    expect(rec.job?.progress.map((e) => e.event)).toContain("stage:verify");
    expect(rec.turns[4]?.jobs_done[0]?.assessment?.n_changed).toBe(1);
    // the old three-turn recording still parses: every addition defaults
    const old = {
      cell_id: CELL,
      lon: 1,
      lat: 2,
      recorded_at: "t",
      model: "m",
      turns: [turn({ route: undefined })],
      values: {},
    };
    expect(RecordedChat.parse(old).job).toBeNull();
  });

  it("refuses a job with a progress event that has no name, and a refusal outside the four reasons", () => {
    const bad = recording();
    bad.job = { ...bad.job, progress: [{ at: "t" }] } as never;
    expect(RecordedChat.safeParse(bad).success).toBe(false);
    const shrug = recording();
    shrug.turns[2] = turn({ abstention: { abstain_id: "a:1", reason: "tired" } });
    expect(RecordedChat.safeParse(shrug).success).toBe(false);
  });

  it("parses the committed recording, when there is one", () => {
    const path = resolve(import.meta.dirname, "../../public/data/prospect/recorded_chat.json");
    if (!existsSync(path)) return;
    const rec = RecordedChat.parse(JSON.parse(readFileSync(path, "utf8")));
    expect(rec.turns.length).toBeGreaterThan(0);
    for (const t of rec.turns) {
      // every cited id the file carries a value for; the browser prints those as chips
      for (const c of t.claims)
        for (const id of c.value_ids) expect(rec.values[id as ValueId], id).toBeDefined();
      if (!t.published) expect(t.text).toBeNull();
    }
  });
});

describe("what the tour reads off a recording", () => {
  it("splits the transcript at the analyst's invocation and names the chain the job made", () => {
    const target = recordedCellTarget(RecordedChat.parse(recording()));
    expect(target).toEqual({
      cellId: CELL,
      lon: -103.7255,
      lat: 58.1579,
      askTurns: 3,
      turns: 5,
      chainId: CHAIN,
      verdict: "insufficient",
      chainPublished: false,
    });
  });

  it("replays the whole transcript as the agent step when no analyst was invoked", () => {
    const rec = RecordedChat.parse({ ...recording(), turns: recording().turns.slice(0, 3), job: null });
    const target = recordedCellTarget(rec);
    expect(target.askTurns).toBe(3);
    expect(target.turns).toBe(3);
    expect(target.chainId).toBeNull();
    expect(target.chainPublished).toBeNull();
  });
});

describe("the chat panel replaying a recording", () => {
  it("draws the route line, the withheld answer, the refusal and the cited chips, and asks nothing of the service", async () => {
    const el = await mount(3);
    const panel = el.querySelector('[data-testid="chat-panel"]');
    expect(panel?.getAttribute("data-replay")).toBe("");
    expect(panel?.textContent).toContain("recorded");
    const turns = el.querySelectorAll('[data-testid="chat-turn"]');
    expect(turns).toHaveLength(3);
    expect(turns[0]?.querySelector('[data-testid="chat-route"]')?.textContent).toContain("cell_features");
    expect(turns[0]?.querySelector(`[data-vid="${CONDUCTOR}"]`)).not.toBeNull();
    expect(turns[1]?.hasAttribute("data-withheld")).toBe(true);
    expect(turns[1]?.textContent).toContain("0.93 is not backed");
    expect(turns[1]?.textContent).toContain("answered on the second ask");
    const refusal = turns[2]?.querySelector('[data-testid="chat-abstention"]');
    expect(refusal?.getAttribute("data-reason")).toBe("out_of_scope");
    expect(refusal?.textContent).toContain("a:1f2e3d4c:1");
    expect(el.querySelector('[data-testid="chat-job"]')).toBeNull();
    expect(serviceCalls).toEqual([]);
  });

  it("draws the job card as its row ended, with its stages, verdict and cost, then the diff on the follow-up", async () => {
    const el = await mount(5);
    const cards = el.querySelectorAll('[data-testid="chat-job"]');
    expect(cards).toHaveLength(2);
    const invoked = cards[0];
    expect(invoked?.getAttribute("data-status")).toBe("done");
    expect(invoked?.querySelector('[data-testid="chat-job-stages"]')?.textContent).toContain(
      "execute · verify · decide",
    );
    expect(invoked?.textContent).toContain("insufficient evidence");
    expect(invoked?.textContent).not.toContain("18000");
    expect(invoked?.querySelector(`[data-vid="c:job:${JOB}:cost_usd"]`)).not.toBeNull();
    const reported = el.querySelector('[data-testid="chat-job-done"]');
    expect(reported?.textContent).toContain("the analyst finished");
    const diff = reported?.querySelector('[data-testid="chat-assessment"]');
    expect(diff?.textContent).toContain("verdict changed");
    expect(diff?.textContent).toContain("supports a closer look");
    expect(diff?.textContent).toContain("fault_proximity");
    expect(diff?.textContent).toContain("met → unknown");
    // a recorded row is final: nothing is polled, nothing is asked
    expect(serviceCalls).toEqual([]);
  });
});

describe("leaving the tour", () => {
  it("hands the chat back to the live agent however the tour ends, not only from the walkthrough card", () => {
    const s = useStore.getState();
    s.setTour({ step: 7, startedAt: Date.now() });
    s.setChatReplay({ cellId: "0145_0019", turns: 3 });
    // the top bar's button and the G key end the tour this way, without the card's exit handler
    useStore.getState().setTour({ step: -1, startedAt: null });
    expect(useStore.getState().chatReplay).toBeNull();
    // moving between steps leaves the replay to the step that set it
    s.setTour({ step: 7, startedAt: Date.now() });
    s.setChatReplay({ cellId: "0145_0019", turns: 3 });
    useStore.getState().setTour({ step: 8 });
    expect(useStore.getState().chatReplay).toEqual({ cellId: "0145_0019", turns: 3 });
    useStore.getState().setTour({ step: -1, startedAt: null });
  });
});
