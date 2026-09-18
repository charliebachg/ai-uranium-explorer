import { V } from "@/components/values/V";
import { COLORS } from "@/config/layers";
import { WORDING } from "@/config/wording";
import { registerBulkFeature } from "@/data/registry";
import { useManifest } from "@/features/shell/ManifestContext";
import { useStore } from "@/state/store";

const DATASET_LABEL = {
  compilation: "Compilation collar",
  geods: "GeoDS drillhole",
  occurrences: "Uranium occurrence",
  deposits: "Uranium deposit footprint",
  readHole: "Hole read from a report",
  footprint: "Assessment file read by this demo",
  cell: "Analysis cell",
} as const;

export function HoverCard() {
  const hover = useStore((s) => s.hover);
  const selected = useStore((s) => s.selected);
  const manifest = useManifest();
  if (!hover) return null;
  if (selected && selected.dataset === hover.dataset && selected.id === hover.id) return null;

  const [x, y] = hover.point;
  const flipX = x > window.innerWidth - 320;
  const flipY = y > window.innerHeight - 200;
  const style = {
    left: flipX ? undefined : x + 16,
    right: flipX ? window.innerWidth - x + 16 : undefined,
    top: flipY ? undefined : y + 16,
    bottom: flipY ? window.innerHeight - y + 16 : undefined,
  };
  const p = hover.props;

  return (
    <div
      className="glass pointer-events-none fixed z-30 w-[270px] rounded-xl px-3.5 py-3 text-[12.5px] shadow-2xl shadow-black/50"
      style={style}
      data-strict="hovercard"
    >
      <div className="mb-1.5 flex items-center gap-2 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">
        <span
          className="size-1.5 rounded-full"
          style={{
            background:
              hover.dataset === "compilation"
                ? COLORS.compilation
                : hover.dataset === "geods"
                  ? COLORS.geods
                  : COLORS.neutral,
          }}
        />
        <span data-testid="hovercard-label">{DATASET_LABEL[hover.dataset]}</span>
      </div>
      {hover.dataset === "compilation" || hover.dataset === "geods" ? (
        <BulkBody
          dataset={hover.dataset}
          id={hover.id}
          props={p}
          retrievedAt={retrievedAt(manifest, hover.dataset)}
          companies={manifest?.dictionaries.companies ?? []}
        />
      ) : hover.dataset === "readHole" ? (
        <div>
          <div className="font-medium text-[14px] text-ink" data-ident>
            {String(p.name ?? "")}
          </div>
          <div className="text-[12px] text-ink-2" data-ident>
            File {String(p.file ?? "")}
          </div>
          <div className="mt-1.5 text-[11.5px] text-ink-3">Click to open the evidence for this hole</div>
        </div>
      ) : hover.dataset === "footprint" ? (
        <div>
          <div className="font-medium text-[14px] text-ink" data-ident>
            File {String(p.file ?? "")}
          </div>
          <div className="text-[12px] text-ink-2">{String(p.era ?? "")}</div>
          <div className="mt-1.5 text-[11.5px] text-ink-3">Click to open the holes read from this file</div>
        </div>
      ) : hover.dataset === "cell" ? (
        <div>
          <div className="font-mono text-[13.5px] text-ink" data-ident>
            {String(p.cid ?? "")}
          </div>
          <div className="mt-1 text-[11.5px] text-ink-3">Click to open the evidence and ask about it</div>
        </div>
      ) : hover.dataset === "occurrences" ? (
        <div className="text-[13.5px] text-ink" data-ident>
          {String(p.name ?? "")}
        </div>
      ) : (
        <div className="text-[13.5px] text-ink" data-ident>
          {String(p.deposit ?? "")}
          {p.zone ? <span className="block text-[12px] text-ink-3">{String(p.zone)}</span> : null}
        </div>
      )}
    </div>
  );
}

function retrievedAt(manifest: ReturnType<typeof useManifest>, dataset: "compilation" | "geods"): string {
  const id = dataset === "compilation" ? "compilation" : "geods_holes";
  return manifest?.sources.find((s) => s.id === id)?.retrieved_at ?? "";
}

function BulkBody({
  dataset,
  id,
  props,
  retrievedAt,
  companies,
}: {
  dataset: "compilation" | "geods";
  id: number;
  props: Record<string, unknown>;
  retrievedAt: string;
  companies: string[];
}) {
  const ids = registerBulkFeature(dataset, id, props, retrievedAt);
  const company = dataset === "compilation" && typeof props.co === "number" ? companies[props.co] : undefined;
  const file = dataset === "geods" ? props.af : props.src;
  return (
    <>
      <div className="font-medium text-[14px] text-ink" data-ident>
        {String(props.n ?? "(unnamed)")}
      </div>
      {company ? <div className="truncate text-[12px] text-ink-2">{company}</div> : null}
      <dl className="mt-2 grid grid-cols-[auto_1fr] gap-x-3 gap-y-1 text-[12px]">
        <dt className="text-ink-3">Year</dt>
        <dd>{ids.y ? <V id={ids.y} /> : <span className="text-ink-3">not recorded</span>}</dd>
        <dt className="text-ink-3">{dataset === "compilation" ? "Length" : "Depth"}</dt>
        <dd>
          {ids.len || ids.td ? (
            <V id={(ids.len ?? ids.td) as string} />
          ) : (
            <span className="text-ink-3">not recorded</span>
          )}
        </dd>
        {file ? (
          <>
            <dt className="text-ink-3">File</dt>
            <dd className="truncate text-ink-2" data-ident>
              {String(file)}
            </dd>
          </>
        ) : null}
      </dl>
      <div className="mt-2 border-line border-t pt-2 text-[11.5px] text-ink-3">{WORDING.noAssays}</div>
    </>
  );
}
