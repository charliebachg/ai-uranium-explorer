import { scaleLinear } from "d3-scale";
import { V } from "@/components/values/V";
import type { Hole } from "@/data/contract";
import { resolveValue } from "@/data/registry";
import { formatNumber } from "@/lib/format";
import { useStore } from "@/state/store";

/**
 * Strip log. Geometry uses pipeline-derived metres (from_m, to_m); text shows values as printed.
 * Assay blocks are equal width and coloured by extraction status: bar length and colour never scale with grade.
 */

const STATUS_FILL = { pass: "#e6f0ff", flag: "#fab219", miss: "#ff5fa2" } as const;

function metres(id: string): number | null {
  const v = resolveValue(id);
  return typeof v?.value === "number" ? v.value : null;
}

export function StripLog({ hole, onOpen }: { hole: Hole; onOpen: (id: string) => void }) {
  const hover = useStore((s) => s.hoverInterval);
  const setHover = useStore((s) => s.setHoverInterval);

  const cols = [
    {
      key: "prov",
      label: "Provincial lithology",
      rows: hole.provincial_lith.map((r) => ({
        id: r.id,
        from: r.from_m,
        to: r.to_m,
        label: r.description ?? r.code,
        status: null as null | "pass" | "flag" | "miss",
      })),
    },
    {
      key: "lith",
      label: "Extracted lithology",
      rows: hole.lith.map((r) => ({
        id: r.id,
        from: r.from_m,
        to: r.to_m,
        label: r.description ?? r.code,
        status: r.status,
      })),
    },
    {
      key: "assay",
      label: "Assays and probe",
      rows: hole.assays.map((r) => ({
        id: r.id,
        from: r.from_m,
        to: r.to_m,
        label: r.grades[0]?.value ?? null,
        status: r.status,
      })),
    },
  ].filter((c) => c.rows.length);

  const depths = cols
    .flatMap((c) => c.rows.flatMap((r) => [metres(r.from), metres(r.to)]))
    .filter((d): d is number => d !== null);
  if (!depths.length) return null;
  const lo = Math.min(...depths);
  const hi = Math.max(...depths);
  const pad = Math.max((hi - lo) * 0.08, 0.5);
  const height = 220;
  const y = scaleLinear()
    .domain([lo - pad, hi + pad])
    .range([12, height - 8]);
  const ticks = y.ticks(6);
  const colW = 86;
  const axisW = 44;
  const width = axisW + cols.length * (colW + 8);

  return (
    <figure className="rounded-lg border border-line bg-black/20 p-2">
      <figcaption className="mb-1 flex items-center justify-between px-1 text-[10.5px] text-ink-3">
        <span>Strip log (depth in metres; values as printed)</span>
      </figcaption>
      <div className="overflow-x-auto">
        <svg width={width} height={height + 16} role="img" aria-label="Strip log">
          <title>Strip log</title>
          {ticks.map((t) => (
            <g key={t} data-axis>
              <line x1={axisW - 6} x2={width} y1={y(t)} y2={y(t)} stroke="rgba(255,255,255,0.05)" />
              <text
                x={axisW - 10}
                y={y(t) + 3}
                textAnchor="end"
                fontSize="10"
                fill="#6b7785"
                className="tabular"
              >
                {formatNumber(t, "m1")}
              </text>
            </g>
          ))}
          {cols.map((c, i) => {
            const x = axisW + i * (colW + 8);
            return (
              <g key={c.key}>
                <text x={x + colW / 2} y={height + 12} textAnchor="middle" fontSize="9.5" fill="#6b7785">
                  {c.label}
                </text>
                <rect
                  x={x}
                  y={y(lo - pad)}
                  width={colW}
                  height={y(hi + pad) - y(lo - pad)}
                  fill="rgba(255,255,255,0.02)"
                  rx={4}
                />
                {c.rows.map((r) => {
                  const f = metres(r.from);
                  const t = metres(r.to);
                  if (f === null || t === null) return null;
                  const top = y(Math.min(f, t));
                  const h = Math.max(y(Math.max(f, t)) - top, 3);
                  const hot = hover === r.id;
                  const fill = r.status ? STATUS_FILL[r.status] : "#8b97a6";
                  return (
                    // biome-ignore lint/a11y/noStaticElementInteractions: hover sync with the table; the table row is the keyboard path
                    <g key={r.id} onMouseEnter={() => setHover(r.id)} onMouseLeave={() => setHover(null)}>
                      <rect
                        x={x + 2}
                        y={top}
                        width={colW - 4}
                        height={h}
                        rx={2}
                        fill={fill}
                        fillOpacity={hot ? 0.55 : r.status ? 0.26 : 0.14}
                        stroke={fill}
                        strokeOpacity={hot ? 1 : 0.6}
                        strokeWidth={hot ? 1.5 : 1}
                      />
                    </g>
                  );
                })}
              </g>
            );
          })}
        </svg>
      </div>
      {hover ? <HoverLegend hole={hole} id={hover} onOpen={onOpen} /> : null}
    </figure>
  );
}

function HoverLegend({ hole, id, onOpen }: { hole: Hole; id: string; onOpen: (id: string) => void }) {
  const a = hole.assays.find((x) => x.id === id);
  const l = hole.lith.find((x) => x.id === id) ?? hole.provincial_lith.find((x) => x.id === id);
  const from = a?.from ?? l?.from;
  const to = a?.to ?? l?.to;
  return (
    <div className="mt-1 flex flex-wrap items-center gap-1.5 px-1 text-[11.5px] text-ink-2">
      {from ? <V id={from} onSelect={onOpen} /> : null}
      {to && to !== from ? (
        <>
          <span className="text-ink-3">to</span>
          <V id={to} onSelect={onOpen} />
        </>
      ) : null}
      {a?.grades.map((g) => (
        <V key={g.value} id={g.value} onSelect={onOpen} />
      ))}
      {l?.description ? <V id={l.description} /> : null}
    </div>
  );
}
