import { PlugZap } from "lucide-react";
import { SERVICE_COMMAND, SERVICE_ROOT } from "./service";

/** unknown while the health check is in flight, then up or down. */
export type ServiceState = "unknown" | "up" | "down";

/**
 * The local service is a process on the reader's own machine, and not running is its ordinary state. Saying so
 * plainly is better than an error: the map, the metric table and the exported cell file above are static files
 * and carry on working without it.
 */
export function OfflineNotice({ state }: { state: ServiceState }) {
  if (state === "unknown") {
    return (
      <p className="mt-3 text-[12.5px] text-ink-3" data-testid="service-checking">
        Connecting…
      </p>
    );
  }
  return (
    <div
      className="mt-3 flex flex-wrap items-center gap-2 rounded-xl border border-line bg-black/20 px-3 py-2 text-[12.5px] text-ink-2"
      data-testid="service-offline"
      title={SERVICE_ROOT}
    >
      <PlugZap className="size-4 shrink-0 text-st-flag" aria-hidden="true" />
      Service offline. Start it with
      <code className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-ink-2">{SERVICE_COMMAND}</code>
    </div>
  );
}
