import { Link } from "wouter";
import { COLORS, divergingSwatches, scoreRamp } from "@/config/layers";
import { useStore } from "@/state/store";

/**
 * What the colour on the cells means, while it means anything at all.
 *
 * The map's standing rule is that colour encodes a source or a status. The score cells are the one declared
 * exception, so they carry the one legend: without it the ramp is just "darker and lighter", which is exactly
 * how a viewer talks themselves into reading it as good ground and bad ground.
 */
export function ScoreLegend() {
  const on = useStore((s) => s.visible.prospect);
  const model = useStore((s) => s.scoreModel);
  const theme = useStore((s) => s.theme);
  if (!on) return null;
  const diverging = model === "difference";
  return (
    <div
      className="glass pointer-events-auto flex items-center gap-3 rounded-xl px-3 py-2 text-[11px] text-ink-3"
      data-testid="score-legend"
    >
      <span className="text-ink-2">{diverging ? "learned − effort" : `${model} score`}</span>
      <span className="flex items-center gap-1.5">
        <span data-axis>{diverging ? "effort leads" : "0"}</span>
        <span className="flex overflow-hidden rounded-sm border border-line">
          {(diverging ? divergingSwatches(theme) : scoreRamp(theme)).map((c: string) => (
            <span key={c} className="block size-3" style={{ background: c }} />
          ))}
        </span>
        <span data-axis>{diverging ? "learned leads" : "1"}</span>
      </span>
      <span className="flex items-center gap-1.5">
        <span
          className="block size-2.5 rounded-full border"
          style={{ borderColor: COLORS.neutral, opacity: 0.6 }}
        />
        no score for this model: a gap, not a low score
      </span>
      <Link
        href="/eval"
        className="ml-1 text-ink-2 underline decoration-line-strong underline-offset-4 hover:text-ink"
      >
        what these are worth
      </Link>
    </div>
  );
}
