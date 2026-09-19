import { useQuery } from "@tanstack/react-query";
import type { ServiceState } from "./OfflineNotice";
import { candidates, conversation, conversations, evidence, health } from "./service";

/** Query keys, in one place, so an invalidation after a chat turn names the same thing the hook does. */
export const keys = {
  health: ["service", "health"] as const,
  candidates: (limit: number) => ["service", "candidates", limit] as const,
  evidence: (cellId: string) => ["service", "evidence", cellId] as const,
  conversations: (cellId: string) => ["service", "conversations", cellId] as const,
  conversation: (id: string) => ["service", "conversation", id] as const,
};

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
