import createClient from "openapi-fetch";
import type { paths } from "./schema";

/**
 * Where the service is. In development it is `ue prospect serve` on localhost; in the container image the API
 * serves the built site itself, so the build sets VITE_SERVICE_ROOT to "" and calls go to the same origin. A
 * static site with no service is a state every caller shows, not an error.
 */
export const SERVICE_ROOT: string = import.meta.env.VITE_SERVICE_ROOT ?? "http://127.0.0.1:8787";

/**
 * The API key, kept in this browser only. With no key the app works as it always has: a loopback client with
 * no register configured is the `local` principal with every role, and a page never needs to know. With a key
 * register on the server, every call that writes (the chat, a job) carries the key as `X-Api-Key`; the server
 * answers 401 or 403 with a plain reason otherwise. The key never appears in a URL or a log; localStorage can
 * be missing or refuse (a private window, blocked site data), so every access is guarded and a failure reads
 * as "no key".
 */
export const KEY_STORAGE = "ue.apiKey";
export const KEY_HEADER = "X-Api-Key";

export function readKey(): string | null {
  try {
    const raw = window.localStorage.getItem(KEY_STORAGE);
    const key = raw?.trim() ?? "";
    return key ? key : null;
  } catch {
    return null;
  }
}

export function writeKey(key: string | null): void {
  try {
    const clean = key?.trim() ?? "";
    if (clean) window.localStorage.setItem(KEY_STORAGE, clean);
    else window.localStorage.removeItem(KEY_STORAGE);
  } catch {
    // nowhere to keep it: the key lasts until the page is closed, which the dialog says
  }
  for (const listener of listeners) listener();
}

const listeners = new Set<() => void>();
/** Notified after every `writeKey`, so a control showing the key's state re-reads it. */
export function subscribeKey(listener: () => void): () => void {
  listeners.add(listener);
  return () => listeners.delete(listener);
}

/** The header to send, for a caller that builds its own request (the NDJSON stream uses fetch directly). */
export function authHeaders(key: string | null = readKey()): Record<string, string> {
  return key ? { [KEY_HEADER]: key } : {};
}

/**
 * A client typed from the service's own OpenAPI document (`ue api-spec` → openapi-typescript). Route paths,
 * parameters and response shapes are checked at compile time; the zod contract in data/contract.ts still
 * validates every response at run time, because a type is a promise and a parse is a check.
 */
export const api = createClient<paths>({ baseUrl: SERVICE_ROOT });

/** The key goes on at request time, not at client creation, so a key set in the dialog applies at once. */
export function withKey(request: Request, key: string | null = readKey()): Request {
  if (key && !request.headers.has(KEY_HEADER)) request.headers.set(KEY_HEADER, key);
  return request;
}

api.use({
  onRequest({ request }) {
    return withKey(request);
  },
});
