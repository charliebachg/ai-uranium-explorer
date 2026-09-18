import { AlertTriangle, Info } from "lucide-react";
import { useEffect, useState } from "react";
import { prospectBanner, WORDING } from "@/config/wording";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";

/**
 * Never dismissible, never truncated. After 6 s it only dims slightly.
 *
 * It also has to tell the truth about the layer that is actually on. The standing line says colour encodes
 * extraction status and not prospectivity, which stops being true the moment the score cells are drawn — that
 * is the one layer allowed to colour by a value. So turning them on swaps the banner for the one that says so.
 */
export function HonestyBanner() {
  const scoresOn = useStore((s) => s.visible.prospect);
  const [settled, setSettled] = useState(false);
  useEffect(() => {
    const t = window.setTimeout(() => setSettled(true), 6000);
    return () => window.clearTimeout(t);
  }, []);
  return (
    <div
      role="note"
      data-testid="honesty-banner"
      className={cn(
        "glass pointer-events-auto flex max-w-[640px] items-start gap-2 rounded-full px-4 py-2 text-[12.5px] shadow-xl shadow-black/30 transition-opacity duration-700",
        scoresOn ? "border-st-flag/30 text-st-flag" : "text-ink-2",
        settled && !scoresOn ? "opacity-80 hover:opacity-100" : "opacity-100",
      )}
      data-scores={scoresOn ? "" : undefined}
    >
      {scoresOn ? (
        <AlertTriangle className="mt-[1px] size-3.5 shrink-0" aria-hidden="true" />
      ) : (
        <Info className="mt-[1px] size-3.5 shrink-0 text-ink-3" aria-hidden="true" />
      )}
      <span>{scoresOn ? prospectBanner : WORDING.banner}</span>
    </div>
  );
}
