import { TriangleAlert, X } from "lucide-react";
import { useStore } from "@/state/store";

export function MapErrorToast({ dataError }: { dataError: string | null }) {
  const mapError = useStore((s) => s.mapError);
  const setMapError = useStore((s) => s.setMapError);
  const message = dataError ?? mapError;
  if (!message) return null;
  return (
    <div
      role="alert"
      className="glass pointer-events-auto absolute bottom-24 left-1/2 z-40 flex max-w-[560px] -translate-x-1/2 items-start gap-2.5 rounded-xl border-st-flag/30 px-4 py-3 text-[12.5px] text-ink-2 shadow-2xl"
    >
      <TriangleAlert className="mt-0.5 size-4 shrink-0 text-st-flag" />
      <span className="min-w-0 break-words">{message}</span>
      {!dataError ? (
        <button
          type="button"
          onClick={() => setMapError(null)}
          className="rounded p-0.5 text-ink-3 hover:text-ink"
          aria-label="Dismiss"
        >
          <X className="size-3.5" />
        </button>
      ) : null}
    </div>
  );
}
