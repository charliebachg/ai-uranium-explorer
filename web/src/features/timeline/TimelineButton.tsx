import { BarChart3 } from "lucide-react";
import { WORDING } from "@/config/wording";
import { useStore } from "@/state/store";

/** Opens the timeline HUD. Hidden while the timeline or the walkthrough already occupies the bottom strip. */
export function TimelineButton() {
  const open = useStore((s) => s.timeline.open);
  const tourActive = useStore((s) => s.tour.step >= 0);
  const setTimeline = useStore((s) => s.setTimeline);
  if (open || tourActive) return null;
  return (
    <button
      type="button"
      onClick={() => setTimeline({ open: true })}
      title={WORDING.timelineCaption}
      className="glass pointer-events-auto flex items-center gap-2 rounded-full px-3.5 py-2 text-[12px] text-ink-2 shadow-xl shadow-black/30 transition-colors hover:text-ink"
    >
      <BarChart3 className="size-3.5 text-ink-3" aria-hidden="true" />
      Timeline
      <kbd className="rounded bg-white/[0.07] px-1.5 py-0.5 text-[10px] text-ink-3">T</kbd>
    </button>
  );
}
