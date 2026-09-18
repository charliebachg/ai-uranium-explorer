import { useStore } from "@/state/store";

/** States the exaggeration of the shift-arrow field: the vectors are not drawn to scale at regional zooms. */
export function ShiftFieldLegend() {
  const on = useStore((s) => s.visible.datumField);
  const factor = useStore((s) => s.arrowExaggeration);
  if (!on) return null;
  return (
    <div className="glass rounded-lg px-2.5 py-1.5 text-[11px] text-ink-2" data-instrument="shift-field">
      NAD27 to NAD83 arrows{" "}
      <span className="text-ink-3">
        {factor > 1
          ? `· length proportional to the shift, exaggerated about x${factor} at this zoom`
          : "· to scale"}
      </span>
    </div>
  );
}
