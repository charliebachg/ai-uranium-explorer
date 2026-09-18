import { z } from "zod";
import { Candidate, CellEvidence, ChatResponse } from "@/data/contract";
import { registerValues } from "@/data/registry";
import { registerCandidateScores, registerCriterionWeights, registerKnownShares } from "./cellValues";

/**
 * The local agent service (`lr prospect serve`). The published site is static files; a conversation is not, so
 * the evidence record and the chat come from a process on localhost that may simply not be running. Every call
 * here reports that as a state rather than an error, and every response is validated against the contract and
 * its value registry registered before anything is drawn.
 */

export const SERVICE_ROOT = "http://127.0.0.1:8787";
export const SERVICE_COMMAND = "lr prospect serve";

const HEALTH_TIMEOUT_MS = 2500;
const READ_TIMEOUT_MS = 15_000;

async function getJson(path: string, timeoutMs: number): Promise<unknown> {
  const res = await fetch(`${SERVICE_ROOT}${path}`, { signal: AbortSignal.timeout(timeoutMs) });
  if (!res.ok) throw new Error(`${path}: HTTP ${res.status}`);
  return res.json();
}

function parsed<S extends z.ZodTypeAny>(path: string, schema: S, raw: unknown): z.infer<S> {
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
    const raw = (await getJson("/api/health", HEALTH_TIMEOUT_MS)) as { ok?: unknown };
    return raw?.ok === true;
  } catch {
    return false;
  }
}

/** The cells the criteria model ranks highest, with the context that says whether the ranking means anything. */
export async function candidates(limit: number): Promise<Candidate[]> {
  const raw = await getJson(`/api/cells?limit=${limit}`, READ_TIMEOUT_MS);
  const cells = parsed("/api/cells", z.object({ cells: z.array(Candidate) }), raw).cells;
  registerCandidateScores(cells);
  return cells;
}

export async function evidence(cellId: string): Promise<CellEvidence> {
  const raw = await getJson(`/api/cell/${cellId}`, READ_TIMEOUT_MS);
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
  | { type: "error"; error: string };

/**
 * The same question, but reported as it happens: one JSON object per line, flushed by the service as each
 * step completes. Watching the agent decide it needs the criteria breakdown and then the label context is most
 * of what makes it legible; a spinner teaches nobody anything.
 */
export async function askStreaming(
  body: { cell_id: string; question: string; conversation_id?: string },
  onEvent: (e: ChatEvent) => void,
): Promise<ChatResponse> {
  const res = await fetch(`${SERVICE_ROOT}/api/chat/stream`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
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
  const answer = parsed("/api/chat/stream", ChatResponse, done);
  registerValues(answer.values);
  return answer;
}

export async function ask(body: {
  cell_id: string;
  question: string;
  conversation_id?: string;
}): Promise<ChatResponse> {
  const res = await fetch(`${SERVICE_ROOT}/api/chat`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(body),
  });
  const raw = (await res.json()) as { error?: string };
  if (!res.ok) throw new Error(raw?.error ? String(raw.error) : `/api/chat: HTTP ${res.status}`);
  const answer = parsed("/api/chat", ChatResponse, raw);
  registerValues(answer.values);
  return answer;
}
