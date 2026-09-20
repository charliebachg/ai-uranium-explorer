import { useQuery } from "@tanstack/react-query";
import type { ServiceState } from "./OfflineNotice";
import { candidates, conversation, conversations, evidence, health, job } from "./service";

/** Query keys, in one place, so an invalidation after a chat turn names the same thing the hook does. */
export const keys = {
  health: ["service", "health"] as const,
  candidates: (limit: number) => ["service", "candidates", limit] as const,
  evidence: (cellId: string) => ["service", "evidence", cellId] as const,
  conversations: (cellId: string) => ["service", "conversations", cellId] as const,
  conversation: (id: string) => ["service", "conversation", id] as const,
  job: (id: string) => ["service", "job", id] as const,
};

/** A job is polled while it is queued or running and left alone once it has reached a final state. */
export const JOB_POLL_MS = 3000;
export function jobFinished(status: string | undefined): boolean {
  return status === "done" || status === "failed" || status === "cancelled";
}

/** Three states, not two: the rail must not say "down" while the first probe is still in flight. */
export function serviceState(q: { isPending: boolean; data?: boolean }): ServiceState {
  if (q.isPending) return "unknown";
  return q.data ? "up" : "down";
}

export function useServiceHealth() {
  return useQuery({ queryKey: keys.health, queryFn: health, refetchInterval: 30_000, staleTime: 10_000 });
}

export function useCandidates(limit: number, enabled: boolean) {
  return useQuery({ queryKey: keys.candidates(limit), queryFn: () => candidates(limit), enabled });
}

export function useEvidence(cellId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: keys.evidence(cellId ?? ""),
    queryFn: () => evidence(cellId as string),
    enabled: enabled && !!cellId,
  });
}

export function useConversations(cellId: string | null, enabled: boolean) {
  return useQuery({
    queryKey: keys.conversations(cellId ?? ""),
    queryFn: () => conversations(cellId as string),
    enabled: enabled && !!cellId,
  });
}

export function useConversation(id: string | null) {
  return useQuery({
    queryKey: keys.conversation(id ?? ""),
    queryFn: () => conversation(id as string),
    enabled: !!id,
  });
}

/** One analyst job, polled until it finishes; the transcript's job card and the chains query hang off it. */
export function useJob(id: string | null, enabled: boolean) {
  return useQuery({
    queryKey: keys.job(id ?? ""),
    queryFn: () => job(id as string),
    enabled: enabled && !!id,
    refetchInterval: (q) => (jobFinished(q.state.data?.status) ? false : JOB_POLL_MS),
  });
}
