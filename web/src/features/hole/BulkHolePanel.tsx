import { Crosshair, X } from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { V } from "@/components/values/V";
import { COLORS } from "@/config/layers";
import { WORDING } from "@/config/wording";
import { registerBulkFeature } from "@/data/registry";
import { useManifest } from "@/features/shell/ManifestContext";
import { mapController } from "@/map/MapView";
import { useStore } from "@/state/store";

export function BulkHolePanel() {
  const selected = useStore((s) => s.selected);
  const select = useStore((s) => s.select);
  const manifest = useManifest();

  const show = selected && (selected.dataset === "compilation" || selected.dataset === "geods");

  return (
    <AnimatePresence>
      {show && selected ? (
        <motion.aside
          key={`${selected.dataset}:${selected.id}`}
          initial={{ opacity: 0, x: 24 }}
          animate={{ opacity: 1, x: 0 }}
          exit={{ opacity: 0, x: 24 }}
          transition={{ type: "spring", stiffness: 380, damping: 34 }}
          className="glass pointer-events-auto w-[380px] rounded-2xl shadow-2xl shadow-black/50"
          data-strict="hole-panel"
          aria-label="Selected drillhole"
        >
          <Body selected={selected} manifest={manifest} onClose={() => select(null)} />
        </motion.aside>
      ) : null}
    </AnimatePresence>
  );
}

function Body({
  selected,
  manifest,
  onClose,
}: {
  selected: NonNullable<ReturnType<typeof useStore.getState>["selected"]>;
  manifest: ReturnType<typeof useManifest>;
  onClose: () => void;
}) {
  const dataset = selected.dataset as "compilation" | "geods";
  const sourceId = dataset === "compilation" ? "compilation" : "geods_holes";
  const source = manifest?.sources.find((s) => s.id === sourceId);
  const ids = registerBulkFeature(dataset, selected.id, selected.props, source?.retrieved_at ?? "");
  const p = selected.props;
  const company =
    dataset === "compilation" && typeof p.co === "number"
      ? manifest?.dictionaries.companies[p.co]
      : undefined;
  const color = dataset === "compilation" ? COLORS.compilation : COLORS.geods;

  const rows: { label: string; node: React.ReactNode }[] = [
    { label: "Year drilled", node: ids.y ? <V id={ids.y} /> : <Missing /> },
    dataset === "compilation"
      ? { label: "Hole length", node: ids.len ? <V id={ids.len} /> : <Missing /> }
      : { label: "Measured depth", node: ids.td ? <V id={ids.td} /> : <Missing /> },
    { label: "Azimuth", node: ids.az ? <V id={ids.az} /> : <Missing /> },
    dataset === "compilation"
      ? { label: "Inclination", node: ids.dip ? <V id={ids.dip} /> : <Missing /> }
      : { label: "Inclination", node: ids.inc ? <V id={ids.inc} /> : <Missing /> },
  ];

  return (
    <div className="p-4">
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="mb-1 flex items-center gap-2 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">
            <span
              className="size-2 rounded-full"
              style={{ background: color, boxShadow: `0 0 8px ${color}` }}
            />
            {dataset === "compilation" ? "Compilation collar" : "GeoDS drillhole"}
          </div>
          <h2 className="truncate font-semibold text-[18px] text-ink tracking-tight" data-ident>
            {String(p.n ?? "(unnamed)")}
          </h2>
          {company ? <div className="truncate text-[12.5px] text-ink-2">{company}</div> : null}
        </div>
        <div className="flex shrink-0 gap-1">
          <IconButton label="Centre on hole" onClick={() => mapController()?.flyTo(selected.lngLat, 13)}>
            <Crosshair className="size-4" />
          </IconButton>
          <IconButton label="Close (Esc)" onClick={onClose}>
            <X className="size-4" />
          </IconButton>
        </div>
      </div>

      <dl className="mt-4 grid grid-cols-2 gap-2">
        {rows.map((r) => (
          <div key={r.label} className="rounded-xl bg-black/25 px-3 py-2.5">
            <dt className="text-[11px] text-ink-3">{r.label}</dt>
            <dd className="mt-0.5 text-[15px] text-ink">{r.node}</dd>
          </div>
        ))}
      </dl>

      {dataset === "geods" && p.af ? (
        <Field label="Assessment file" value={String(p.af)} />
      ) : dataset === "compilation" && p.src ? (
        <Field label="Source recorded in the compilation" value={String(p.src)} />
      ) : null}

      <div className="mt-4 space-y-2 rounded-xl border border-line bg-white/[0.02] p-3 text-[12px]">
        <div className="text-ink-2">{WORDING.noAssays}</div>
        <div className="text-ink-3">{WORDING.notRead}</div>
      </div>

      <div className="mt-3 text-[11px] text-ink-3 leading-relaxed" data-ident="source">
        {source ? `${source.title}. ${source.licence}.` : null}
      </div>
    </div>
  );
}

function Field({ label, value }: { label: string; value: string }) {
  return (
    <div className="mt-3 rounded-xl bg-black/25 px-3 py-2.5">
      <div className="text-[11px] text-ink-3">{label}</div>
      <div className="mt-0.5 truncate text-[13.5px] text-ink tabular" data-ident>
        {value}
      </div>
    </div>
  );
}

function Missing() {
  return <span className="text-[13px] text-ink-3">not recorded</span>;
}

function IconButton({
  label,
  onClick,
  children,
}: {
  label: string;
  onClick: () => void;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      className="rounded-lg p-1.5 text-ink-3 transition-colors hover:bg-white/[0.06] hover:text-ink"
    >
      {children}
    </button>
  );
}
