import { PlugZap } from "lucide-react";
import { SERVICE_COMMAND, SERVICE_ROOT } from "./service";

/** unknown while the health check is in flight, then up or down. */
export type ServiceState = "unknown" | "up" | "down";

/**
 * The local service is a process on the reader's own machine, and not running is its ordinary state. Saying so
 * plainly is better than an error: the map, the metric table and the exported cell file above are static files
 * and carry on working without it.
 */
export function OfflineNotice({ state, what }: { state: ServiceState; what: string }) {
  if (state === "unknown") {
    return (
      <p className="mt-3 text-[12.5px] text-ink-3" data-testid="service-checking">
        Looking for the cell service…
      </p>
    );
  }
  return (
    <div
      className="mt-3 rounded-xl border border-line bg-black/20 p-3 text-[12.5px] text-ink-2"
      data-testid="service-offline"
    >
      <p className="flex items-center gap-2 text-ink">
        <PlugZap className="size-4 shrink-0 text-st-flag" aria-hidden="true" />
        The cell service is not running.
      </p>
      <p className="mt-1.5 text-[11.5px] text-ink-3">
        {what} Start it with{" "}
        <code className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-ink-2">{SERVICE_COMMAND}</code>{" "}
        and reload; it answers on <span data-ident>{SERVICE_ROOT}</span>. Nothing else on this page needs it:
        the map, the legend and the fold tests are static files.
      </p>
    </div>
  );
}
