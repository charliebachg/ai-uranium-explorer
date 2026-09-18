import { FlaskConical } from "lucide-react";
import { FIXTURE_MODE } from "@/data/reports";

/**
 * Shown whenever report data comes from the development fixture rather than a pipeline run. It sits in the top bar
 * so it is visible in every state without covering evidence.
 */
export function FixtureBadge() {
  if (!FIXTURE_MODE) return null;
  return (
    <span
      role="note"
      title="Report values were hand-keyed from real public pages for UI development. They are not model output."
      className="flex items-center gap-1.5 rounded-lg border border-st-flag/40 bg-[#1c1606] px-2.5 py-1.5 text-[11.5px] text-st-flag"
      data-testid="fixture-watermark"
    >
      <FlaskConical className="size-3.5" />
      <span className="font-semibold tracking-wider">FIXTURE</span>
      <span className="text-st-flag/80">hand-keyed, not model output</span>
    </span>
  );
}
