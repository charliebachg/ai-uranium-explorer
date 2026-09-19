import createClient from "openapi-fetch";
import type { paths } from "./schema";

/** The service on localhost. The published static site has no service; every caller treats that as a state. */
export const SERVICE_ROOT = "http://127.0.0.1:8787";

/**
 * A client typed from the service's own OpenAPI document (`lr api-spec` → openapi-typescript). Route paths,
 * parameters and response shapes are checked at compile time; the zod contract in data/contract.ts still
 * validates every response at run time, because a type is a promise and a parse is a check.
 */
export const api = createClient<paths>({ baseUrl: SERVICE_ROOT });
