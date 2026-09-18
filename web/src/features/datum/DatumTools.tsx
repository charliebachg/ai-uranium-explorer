import { Compass, Crosshair, Move } from "lucide-react";
import { V } from "@/components/values/V";
import { useManifest } from "@/features/shell/ManifestContext";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";

/** Datum tools in the left rail: the cursor lens, the shift field and the misread-datum comparison. */
export function DatumTools() {
  const datum = useStore((s) => s.datum);
  const setDatum = useStore((s) => s.setDatum);
  const visible = useStore((s) => s.visible);
  const toggleLayer = useStore((s) => s.toggleLayer);
  const manifest = useManifest();

  return (
    <section className="mb-1">
      <div className="px-2 pt-2 pb-1 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">Datum tools</div>
      <Row
        icon={<Crosshair className="size-3.5" />}
        label="Cursor lens"
        hint="Shift vector at the cursor"
        kbd="D"
        on={datum.lens}
        onClick={() => setDatum({ lens: !datum.lens })}
      />
      <Row
        icon={<Compass className="size-3.5" />}
        label="Shift field"
        hint="NAD27 to NAD83 arrows"
        on={visible.datumField}
        onClick={() => toggleLayer("datumField")}
      />
      <Row
        icon={<Move className="size-3.5" />}
        label="Misread datum"
        hint="Where NAD27 collars land if read as NAD83"
        kbd="M"
        on={datum.misread}
        onClick={() => setDatum({ misread: !datum.misread })}
      />
      {manifest?.ref_points.length ? (
        <div className="mx-2 mt-1 rounded-lg bg-black/25 px-2.5 py-2 text-[11px] text-ink-3">
          {manifest.ref_points.map((r) => (
            <div key={r.label} className="flex items-center justify-between gap-2">
              <span className="truncate">{r.label}</span>
              <V id={r.shift_m} className="text-ink-2" />
            </div>
          ))}
        </div>
      ) : null}
    </section>
  );
}

function Row({
  icon,
  label,
  hint,
  kbd,
  on,
  onClick,
}: {
  icon: React.ReactNode;
  label: string;
  hint: string;
  kbd?: string;
  on: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onClick}
      className={cn(
        "flex w-full items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors hover:bg-white/[0.04]",
        !on && "opacity-70",
      )}
    >
      <span
        className={cn(
          "flex size-6 shrink-0 items-center justify-center rounded-md",
          on ? "bg-src-compilation/25 text-st-pass" : "bg-black/30 text-ink-3",
        )}
      >
        {icon}
      </span>
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] text-ink">{label}</span>
        <span className="block truncate text-[11px] text-ink-3">{hint}</span>
      </span>
      {kbd ? <kbd className="rounded bg-white/10 px-1 text-[10px] text-ink-3">{kbd}</kbd> : null}
    </button>
  );
}
