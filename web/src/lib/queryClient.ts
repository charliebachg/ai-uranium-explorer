import { QueryClient } from "@tanstack/react-query";

/**
 * Server state lives here; zustand keeps UI state only. Service queries never retry on their own: a service
 * that is down is a state the rail shows, not a condition to hide behind three silent attempts.
 */
export const queryClient = new QueryClient({
  defaultOptions: {
    queries: { retry: false, staleTime: 60_000, refetchOnWindowFocus: false },
  },
});
