import createClient from "openapi-fetch";
import type { paths } from "./schema";

/**
 * Where the service is. In development it is `lr prospect serve` on localhost; in the container image the API
 * serves the built site itself, so the build sets VITE_SERVICE_ROOT to "" and calls go to the same origin. A
 * static site with no service is a state every caller shows, not an error.
 */
export const SERVICE_ROOT: string = import.meta.env.VITE_SERVICE_ROOT ?? "http://127.0.0.1:8787";

/**
 * A client typed from the service's own OpenAPI document (`lr api-spec` → openapi-typescript). Route paths,
 * parameters and response shapes are checked at compile time; the zod contract in data/contract.ts still
 * validates every response at run time, because a type is a promise and a parse is a check.
 */
export const api = createClient<paths>({ baseUrl: SERVICE_ROOT });
