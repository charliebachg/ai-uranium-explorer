import { authHeaders, SERVICE_ROOT } from "@/api/client";
import { ReviewItem, ReviewQueuePage, type ReviewStatus } from "@/data/contract";

/**
 * The review queue (`/api/review`): what the second reader disagreed with the first about, and the decision a
 * geologist records on each item. Plain fetches rather than the typed client, because the generated client
 * types are regenerated from the OpenAPI document by a tool this checkout does not carry; the zod contract
 * still checks every response before anything is drawn.
 */

const TIMEOUT_MS = 15_000;

export class ReviewError extends Error {
  constructor(
    public status: number,
    detail: string,
  ) {
    super(detail);
  }
}

async function parsedJson<T>(path: string, res: Response, parse: (raw: unknown) => T): Promise<T> {
  if (!res.ok) {
    let detail = `HTTP ${res.status}`;
    try {
      const body = (await res.json()) as { detail?: unknown };
      if (body && typeof body.detail === "string") detail = body.detail;
    } catch {
      // the body was not JSON: the status is the message
    }
    throw new ReviewError(res.status, `${path}: ${detail}`);
  }
  return parse(await res.json());
}

export async function listReview(opts: {
  file?: string | null;
  status?: ReviewStatus | "all";
  limit?: number;
  offset?: number;
}): Promise<ReviewQueuePage> {
  const q = new URLSearchParams();
  if (opts.file) q.set("file", opts.file);
  q.set("status", opts.status ?? "open");
  q.set("limit", String(opts.limit ?? 50));
  q.set("offset", String(opts.offset ?? 0));
  const path = `/api/review?${q.toString()}`;
  const res = await fetch(`${SERVICE_ROOT}${path}`, { signal: AbortSignal.timeout(TIMEOUT_MS) });
  return parsedJson(path, res, (raw) => ReviewQueuePage.parse(raw));
}

export type Decision = Exclude<ReviewStatus, "open">;

export async function resolveReview(
  queueId: string,
  decision: Decision,
  opts: { note?: string; value?: Record<string, unknown> } = {},
): Promise<ReviewItem> {
  const path = `/api/review/${encodeURIComponent(queueId)}`;
  const res = await fetch(`${SERVICE_ROOT}${path}`, {
    method: "POST",
    headers: { "Content-Type": "application/json", ...authHeaders() },
    body: JSON.stringify({ decision, note: opts.note ?? "", value: opts.value ?? null }),
    signal: AbortSignal.timeout(TIMEOUT_MS),
  });
  return parsedJson(path, res, (raw) => ReviewItem.parse(raw));
}
