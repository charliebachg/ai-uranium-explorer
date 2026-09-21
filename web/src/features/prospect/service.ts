import { z } from "zod";
import { api, authHeaders, SERVICE_ROOT } from "@/api/client";
import {
  Candidate,
  CellConversations,
  CellEvidence,
  type ChatAbstention,
  type ChatInsight,
  type ChatJob,
  ChatResponse,
  type ChatRoute,
  ConversationRecord,
  InterfaceChatResponse,
  Job,
} from "@/data/contract";
import { registerValues } from "@/data/registry";
import { registerCandidateScores, registerCriterionWeights, registerKnownShares } from "./cellValues";

/**
 * The local agent service (`ue prospect serve`). The published site is static files; a conversation is not, so
 * the evidence record and the chat come from a process on localhost that may simply not be running. Every call
 * here reports that as a state rather than an error, and every response is validated against the contract and
 * its value registry registered before anything is drawn.
 */

export { SERVICE_ROOT };
export const SERVICE_COMMAND = "ue prospect serve";

const HEALTH_TIMEOUT_MS = 2500;
const READ_TIMEOUT_MS = 15_000;

/** openapi-fetch hands back {data, error, response}; a non-2xx is an error here, never an empty answer. */
export function got<T>(path: string, out: { data?: T; error?: unknown; response: Response }): T {
  if (out.error !== undefined || out.data === undefined) {
    const detail =
      out.error && typeof out.error === "object" && "detail" in out.error
        ? String((out.error as { detail: unknown }).detail)
        : "";
    throw new Error(`${path}: HTTP ${out.response.status}${detail ? ` (${detail})` : ""}`);
  }
  return out.data;
}
export function parsed<S extends z.ZodTypeAny>(path: string, schema: S, raw: unknown): z.infer<S> {
  const out = schema.safeParse(raw);
  if (!out.success) {
    const issues = out.error.issues
      .slice(0, 3)
      .map((i) => `${i.path.join(".")}: ${i.message}`)
      .join("; ");
    throw new Error(`${path} does not match the data contract: ${issues}`);
  }
  return out.data;
}

export async function health(): Promise<boolean> {
  try {
    const out = await api.GET("/api/health", { signal: AbortSignal.timeout(HEALTH_TIMEOUT_MS) });
    return out.data?.ok === true;
  } catch {
    return false;
  }
}

/** The cells the criteria model ranks highest, with the context that says whether the ranking means anything. */
export async function candidates(limit: number): Promise<Candidate[]> {
  const raw = got(
    "/api/cells",
    await api.GET("/api/cells", {
      params: { query: { limit } },
      signal: AbortSignal.timeout(READ_TIMEOUT_MS),
    }),
  );
  const cells = parsed("/api/cells", z.object({ cells: z.array(Candidate) }), raw).cells;
  registerCandidateScores(cells);
  return cells;
}

export async function evidence(cellId: string): Promise<CellEvidence> {
  const raw = got(
    `/api/cell/${cellId}`,
    await api.GET("/api/cell/{cell_id}", {
      params: { path: { cell_id: cellId } },
      signal: AbortSignal.timeout(READ_TIMEOUT_MS),
    }),
  );
  const record = parsed(`/api/cell/${cellId}`, CellEvidence, raw);
  for (const part of Object.values(record.parts)) registerValues(part.values, { notify: false });
  // two numbers the tools report as plain fields; registered here so the panel can print them like any other
  registerKnownShares(record.cell_id, record.parts.cell_scores?.rows ?? []);
  registerCriterionWeights(record.cell_id, record.parts.criteria_breakdown?.rows ?? []);
  registerValues(record.values);
  return record;
}

/** A question about one cell. The model call is slow by nature, so this one carries no timeout. */
/** What the agent is doing right now, as the service reports it step by step. */
export type ChatEvent =
  | { type: "opened"; tools: string[] }
  | { type: "thinking"; step: number; of: number }
  | { type: "reasoning"; step: number; text: string }
  | { type: "delta"; step: number; text: string }
  | {
      type: "tool";
      tool: string;
      args: Record<string, unknown>;
      reasoning?: string;
      rows: number;
      values: number;
    }
  | { type: "tool_error"; tool: string; error: string }
  | { type: "checking"; claims: number }
  /** the gate objected once; the answer is being asked for again with the objections */
  | { type: "refused"; problems: string[] }
  // the interface agent's own steps: the route and its plan, then the actions it took
  | ({ type: "route" } & ChatRoute)
  | ({ type: "abstain" } & ChatAbstention)
  | ({ type: "insight" } & ChatInsight)
  | ({ type: "job" } & ChatJob)
  | { type: "error"; error: string };

/**
 * The same question, but reported as it happens: one JSON object per line, flushed by the service as each
 * step completes. Watching the agent decide it needs the criteria breakdown and then the label context is most
 * of what makes it legible; a spinner teaches nobody anything.
 */
export async function askStreaming(
  body: { cell_id: string; question: string; conversation_id?: string },
  onEvent: (e: ChatEvent) => void,
): Promise<InterfaceChatResponse> {
  const res = await fetch(`${SERVICE_ROOT}/api/chat/stream`, {
    method: "POST",
    // the typed client's middleware does not see this fetch, so the key goes on here
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify(body),
  });
  if (!res.ok || !res.body) throw new Error(`/api/chat/stream: HTTP ${res.status}`);

  const reader = res.body.getReader();
  const decoder = new TextDecoder();
  let buffer = "";
  let done: unknown = null;

  const take = (line: string) => {
    const trimmed = line.trim();
    if (!trimmed) return;
    const event = JSON.parse(trimmed) as { type: string };
    if (event.type === "done") done = event;
    else onEvent(event as ChatEvent);
  };

  for (;;) {
    const { value, done: finished } = await reader.read();
    if (finished) break;
    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";
    for (const line of lines) take(line);
  }
  if (buffer.trim()) take(buffer);

  if (!done) throw new Error("the service closed the stream before answering");
  const answer = parsed("/api/chat/stream", InterfaceChatResponse, done);
  registerValues(answer.values);
  return answer;
}

/** One job's row, for the transcript's job card to poll until the analyst has finished. */
export async function job(jobId: string): Promise<Job> {
  const res = await fetch(`${SERVICE_ROOT}/api/jobs/${encodeURIComponent(jobId)}`, {
    headers: authHeaders(), // the viewer role needs the key when a register is configured
    signal: AbortSignal.timeout(READ_TIMEOUT_MS),
  });
  if (!res.ok) throw new Error(`/api/jobs/${jobId}: HTTP ${res.status}`);
  return parsed(`/api/jobs/${jobId}`, Job, await res.json());
}

export async function ask(body: {
  cell_id: string;
  question: string;
  conversation_id?: string;
}): Promise<ChatResponse> {
  const raw = got("/api/chat", await api.POST("/api/chat", { body }));
  const answer = parsed("/api/chat", ChatResponse, raw);
  registerValues(answer.values);
  return answer;
}

/** The conversations the service has persisted about one cell, newest first. */
export async function conversations(cellId: string): Promise<CellConversations> {
  const raw = got(
    `/api/cell/${cellId}/conversations`,
    await api.GET("/api/cell/{cell_id}/conversations", {
      params: { path: { cell_id: cellId } },
      signal: AbortSignal.timeout(READ_TIMEOUT_MS),
    }),
  );
  return parsed(`/api/cell/${cellId}/conversations`, CellConversations, raw);
}

/** One persisted transcript; its cited values are registered so its numbers print like any other. */
export async function conversation(id: string): Promise<ConversationRecord> {
  const raw = got(
    `/api/conversation/${id}`,
    await api.GET("/api/conversation/{conversation_id}", {
      params: { path: { conversation_id: id } },
      signal: AbortSignal.timeout(READ_TIMEOUT_MS),
    }),
  );
  const record = parsed(`/api/conversation/${id}`, ConversationRecord, raw);
  for (const t of record.turns) registerValues(t.values, { notify: false });
  return record;
}
