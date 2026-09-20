import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { act } from "react";
import { createRoot, type Root } from "react-dom/client";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import {
  ChatResponse,
  type ChatRoute,
  InterfaceChatResponse,
  InterfaceTurn,
  type ValueId,
} from "@/data/contract";
import { registerValues } from "@/data/registry";
import type { ChatEvent } from "@/features/prospect/service";

/**
 * The chat panel with the interface agent behind it (PRD §8.3): the route line, a refusal with its reason, a
 * recorded insight with its id, and a job card that polls until the analyst has finished. The service is
 * mocked at its two calls; `<V>` throws on an unbacked id under vitest, so a render that completes is itself
 * the check that every number on the cards resolves to a registered value.
 */

(globalThis as { IS_REACT_ACT_ENVIRONMENT?: boolean }).IS_REACT_ACT_ENVIRONMENT = true;

const CELL = "0201_0072";
const CONDUCTOR = `c:cell:${CELL}:d_conductor_m`;
const INSIGHT_VALUE = `c:insight:${CELL}:40f37e5a6b:0`;

type Scripted = { events: ChatEvent[]; response: unknown };
const script: Scripted[] = [];
const jobRows: Record<string, unknown>[] = [];

vi.mock("@/features/prospect/service", async () => ({
  SERVICE_ROOT: "http://test",
  SERVICE_COMMAND: "lr prospect serve",
  health: async () => true,
  candidates: async () => [],
  evidence: async () => null,
  conversations: async () => ({ cell_id: CELL, conversations: [] }),
  conversation: async () => null,
  askStreaming: async (_body: unknown, onEvent: (e: ChatEvent) => void) => {
    const next = script.shift();
    if (!next) throw new Error("nothing scripted");
    for (const e of next.events) onEvent(e);
    const answer = InterfaceChatResponse.parse(next.response);
    registerValues(answer.values); // as the real call does, so the cited chips resolve
    return answer;
  },
  job: async () => {
    const row = jobRows.shift() ?? jobRows[0];
    if (!row) throw new Error("no job row scripted");
    return row;
  },
}));

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
    cost_usd: 0.001,
    ...over,
  };
}

function response(t: Record<string, unknown>, values: Record<string, unknown> = {}) {
  return { conversation_id: "conv1", cell_id: CELL, turn: turn(t), values, cost_usd: 0.002 };
}

const ROUTE: ChatRoute = {
  kind: "explain_score",
  topic: null,
  cell_ids: [CELL],
  entities: [],
  out_of_scope: false,
  detail: "",
  reason: "asks why",
  fallback: null,
  meaning: "why the scores are what they are",
  plan: [
    { tool: "cell_scores", args: { cell_id: CELL } },
    { tool: "criteria_breakdown", args: { cell_id: CELL } },
  ],
};

let root: Root | null = null;
let container: HTMLElement | null = null;

function mount(): HTMLElement {
  registerValues({ [CONDUCTOR]: stat(CONDUCTOR, 820, "m1", "m") }, { notify: false });
  container = document.createElement("div");
  document.body.appendChild(container);
  root = createRoot(container);
  const client = new QueryClient({ defaultOptions: { queries: { retry: false } } });
  act(() => {
    root?.render(
      <QueryClientProvider client={client}>
        <ChatPanel cellId={CELL} state="up" />
      </QueryClientProvider>,
    );
  });
  return container;
}

async function send(el: HTMLElement): Promise<void> {
  // the suggestions call send() with their own text: the shortest path into a turn
  const button = el.querySelector<HTMLButtonElement>('[data-testid="chat-panel"] button');
  await act(async () => {
    button?.click();
    await Promise.resolve();
  });
  await act(async () => {
    await new Promise((r) => setTimeout(r, 0));
  });
}

beforeEach(() => {
  script.length = 0;
  jobRows.length = 0;
});

afterEach(() => {
  act(() => root?.unmount());
  container?.remove();
  root = null;
  container = null;
});

describe("the interface turn contract", () => {
  it("keeps the route and the actions that the plain chat response would strip", () => {
    const raw = response({
      text: "The conductor sits close by.",
      route: ROUTE,
      abstention: null,
      insight: null,
      job: null,
      jobs_done: [],
      expert_ids: [],
      retried: true,
    });
    expect("route" in ChatResponse.parse(raw).turn).toBe(false);
    const parsed = InterfaceChatResponse.parse(raw);
    expect(parsed.turn.route?.kind).toBe("explain_score");
    expect(parsed.turn.route?.plan.map((s) => s.tool)).toEqual(["cell_scores", "criteria_breakdown"]);
    expect(parsed.turn.retried).toBe(true);
  });

  it("holds an abstention to the four reasons and defaults what an older turn lacks", () => {
    expect(InterfaceTurn.parse(turn({})).jobs_done).toEqual([]);
    expect(
      InterfaceTurn.safeParse(turn({ abstention: { abstain_id: "a:1", reason: "tired" } })).success,
    ).toBe(false);
    expect(
      InterfaceTurn.parse(turn({ abstention: { abstain_id: "a:1", reason: "no_value" } })).abstention?.detail,
    ).toBe("");
  });
});

describe("the chat panel with the interface agent", () => {
  it("shows the route while working and on the answer, and the retry", async () => {
    script.push({
      events: [
        { type: "opened", tools: ["cell_scores"] },
        { type: "route", ...ROUTE },
        { type: "tool", tool: "cell_scores", args: {}, rows: 1, values: 1 },
        { type: "refused", problems: ["claim 0: the number 9 is not backed"] },
        { type: "checking", claims: 1 },
      ],
      response: response(
        {
          text: `The conductor is 820 m away (${CONDUCTOR}).`,
          claims: [{ text: "820 m", value_ids: [CONDUCTOR] }],
          route: ROUTE,
          retried: true,
        },
        { [CONDUCTOR]: stat(CONDUCTOR, 820, "m1", "m") },
      ),
    });
    const el = mount();
    await send(el);
    const t = el.querySelector('[data-testid="chat-turn"]');
    expect(t?.getAttribute("data-kind")).toBe("explain_score");
    const route = t?.querySelector('[data-testid="chat-route"]');
    expect(route?.textContent).toContain("explain the score");
    expect(route?.textContent).toContain("cell_scores · criteria_breakdown");
    expect(t?.textContent).toContain("answered on the second ask");
    expect(t?.querySelector(`[data-vid="${CONDUCTOR}"]`)).not.toBeNull();
  });

  it("renders a refusal with its reason and its id where the answer would be", async () => {
    const abstention = {
      abstain_id: "a:conv1:1",
      reason: "out_of_scope",
      detail: "",
      said: "this record never says that",
    };
    script.push({
      events: [{ type: "route", ...ROUTE, kind: "lookup", out_of_scope: true, plan: [] }],
      response: response({
        text: "No answer: this record never says that.",
        cannot_answer: true,
        route: { ...ROUTE, kind: "lookup", out_of_scope: true, plan: [] },
        abstention,
        tools_used: ["abstain"],
      }),
    });
    const el = mount();
    await send(el);
    const box = el.querySelector('[data-testid="chat-abstention"]');
    expect(box?.getAttribute("data-reason")).toBe("out_of_scope");
    expect(box?.textContent).toContain("out of scope");
    expect(box?.textContent).toContain("a:conv1:1");
    expect(el.querySelector('[data-testid="chat-route"]')?.textContent).toContain("out of scope");
  });

  it("shows a recorded insight with its expert-tier id and the values minted from it", async () => {
    const insight = {
      expert_id: "e:40f37e5a6b",
      author: "local",
      text: "the conductor continues 600 m north-east",
      value_ids: [INSIGHT_VALUE],
      recorded_at: "2026-09-21T00:00:00+00:00",
    };
    script.push({
      events: [
        { type: "route", ...ROUTE, kind: "record_insight", plan: [] },
        { type: "insight", ...insight },
      ],
      response: response(
        {
          text: "Recorded in the expert tier as e:40f37e5a6b, author local.",
          claims: [{ text: "the numbers in the insight, as written", value_ids: [INSIGHT_VALUE] }],
          route: { ...ROUTE, kind: "record_insight", plan: [] },
          insight,
          tools_used: ["record_insight"],
        },
        { [INSIGHT_VALUE]: stat(INSIGHT_VALUE, 600, "int") },
      ),
    });
    const el = mount();
    await send(el);
    const card = el.querySelector('[data-testid="chat-insight"]');
    expect(card?.textContent).toContain("e:40f37e5a6b");
    expect(card?.textContent).toContain("by local");
    expect(card?.querySelector(`[data-vid="${INSIGHT_VALUE}"]`)).not.toBeNull();
  });

  it("shows an invoked analyst as a job card that polls to done and prints the verdict and the cost", async () => {
    const job = {
      job_id: "job-abc",
      cell_id: CELL,
      reason: "run it with my insight",
      expert_ids: [INSIGHT_VALUE],
      score_ids: [`c:score:${CELL}:learned`],
      budget_usd: 0.5,
      requested_by: "local",
      submitted_at: "2026-09-21T00:00:00+00:00",
      status: "submitted",
    };
    jobRows.push({
      job_id: "job-abc",
      kind: "analyst",
      cell_id: CELL,
      status: "done",
      requested_by: "local",
      args: {},
      created_at: "t",
      started_at: "t",
      finished_at: "t",
      progress: [],
      result: { chain_id: "run:cell", verdict: "supports_closer_look", cost_usd: 0.04 },
      error: null,
      run_id: "run",
    });
    script.push({
      events: [
        { type: "route", ...ROUTE, kind: "run_analyst", plan: [] },
        { type: "job", ...job },
      ],
      response: response({
        text: `Analyst invoked on cell ${CELL}.`,
        route: { ...ROUTE, kind: "run_analyst", plan: [] },
        job,
        tools_used: ["run_analyst"],
      }),
    });
    const el = mount();
    await send(el);
    const card = el.querySelector('[data-testid="chat-job"]');
    expect(card?.textContent).toContain("job-abc");
    expect(card?.textContent).toContain("the insight recorded here");
    // the poll answers "done": the verdict is words and the cost is a registered value
    for (let i = 0; i < 5 && card?.getAttribute("data-status") !== "done"; i++) {
      await act(async () => {
        await new Promise((r) => setTimeout(r, 5));
      });
    }
    expect(card?.getAttribute("data-status")).toBe("done");
    expect(card?.textContent).toContain("supports a closer look");
    expect(card?.textContent).not.toContain("supports_closer_look");
    expect(card?.querySelector('[data-vid="c:job:job-abc:cost_usd"]')).not.toBeNull();
  });

  it("reports a finished job on a later turn with the diff against the stored chain", async () => {
    const done = {
      job_id: "job-abc",
      cell_id: CELL,
      reason: "r",
      expert_ids: [],
      score_ids: [],
      status: "done",
      verdict: "supports_closer_look",
      result: { chain_id: "run:cell", verdict: "supports_closer_look", cost_usd: 0.04 },
      assessment: {
        chain_id: "run:cell",
        baseline_chain_id: "older",
        verdict: { before: "insufficient", after: "supports_closer_look", changed: true },
        nodes: [
          {
            node_id: "n01",
            criterion: "fault",
            kind: "criterion",
            before: "unknown",
            after: "met",
            expert_ids: [INSIGHT_VALUE],
            changed: true,
          },
          {
            node_id: "n02",
            criterion: "host",
            kind: "criterion",
            before: "met",
            after: "met",
            expert_ids: [],
            changed: false,
          },
        ],
        n_changed: 1,
        n_leaning_on_expert: 1,
        expert_ids: [INSIGHT_VALUE],
        note: "node statuses and the verdict of the run with the insight, beside the stored run without it",
      },
    };
    script.push({
      events: [
        { type: "job", ...done },
        { type: "route", ...ROUTE, kind: "other", plan: [] },
      ],
      response: response({
        text: "Nothing new.",
        route: { ...ROUTE, kind: "other", plan: [] },
        jobs_done: [done],
      }),
    });
    const el = mount();
    await send(el);
    const block = el.querySelector('[data-testid="chat-job-done"]');
    expect(block?.textContent).toContain("the analyst finished");
    const diff = block?.querySelector('[data-testid="chat-assessment"]');
    expect(diff?.textContent).toContain("verdict changed");
    expect(diff?.textContent).toContain("insufficient evidence");
    expect(diff?.textContent).toContain("fault");
    expect(diff?.textContent).toContain("unknown → met");
    expect(diff?.textContent).toContain("leans on your insight");
    expect(diff?.textContent).not.toContain("host");
  });
});
