import { Pause, Play, X } from "lucide-react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { V } from "@/components/values/V";
import { WORDING } from "@/config/wording";
import type { YearHistogram } from "@/data/contract";
import { loadYearHistogram } from "@/data/loader";
import { cn } from "@/lib/cn";
import { formatNumber } from "@/lib/format";
import { useStore } from "@/state/store";
import {
  axisTicks,
  barHeight,
  clampYear,
  PLOT_H,
  stackedMax,
  timelineBars,
  yearAtX,
  yearFraction,
} from "./scale";

/** The whole range plays in about this long, whatever the frame rate. */
const PLAY_SECONDS = 9;
/** Axis labels every twenty years: enough to read the shape, few enough to stay legible on a phone. */
const TICK_STEP = 20;

/**
 * Timeline HUD: one bar per year, compilation below and GeoDS above, with a scrubber that sets the latest
 * year the map draws. Colour is data source only. Holes with no year cannot sit anywhere on this axis, so
 * they are counted in the footer rather than dropped.
 */
export function Timeline() {
  const open = useStore((s) => s.timeline.open);
  const [hist, setHist] = useState<YearHistogram | null>(null);
  const [failed, setFailed] = useState(false);

  useEffect(() => {
    if (!open) return;
    let alive = true;
    loadYearHistogram().then(
      (h) => {
        if (alive) setHist(h);
      },
      () => {
        if (alive) setFailed(true);
      },
    );
    return () => {
      alive = false;
    };
  }, [open]);

  if (!open) return null;

  return (
    <section
      data-strict="timeline"
      data-testid="timeline"
      aria-label="Drilling timeline"
      className="glass pointer-events-auto w-[700px] max-w-[calc(100vw-2rem)] rounded-2xl px-4 py-3 shadow-2xl shadow-black/50"
    >
      {failed ? (
        <div className="text-[12px] text-st-miss">The year histogram did not load.</div>
      ) : hist ? (
        <Body h={hist} />
      ) : (
        <Skeleton />
      )}
    </section>
  );
}

/** Quiet placeholder while the histogram loads: the shape of the panel, no spinner, no numbers. */
function Skeleton() {
  return (
    <div role="status" aria-label="Loading the year histogram" className="animate-pulse">
      <div className="h-3.5 w-52 rounded bg-white/[0.07]" />
      <div className="mt-3 rounded bg-white/[0.04]" style={{ height: PLOT_H }} />
      <div className="mt-3 h-2.5 w-2/3 rounded bg-white/[0.05]" />
    </div>
  );
}

function Body({ h }: { h: YearHistogram }) {
  const bars = useMemo(() => timelineBars(h), [h]);
  const max = useMemo(() => stackedMax(bars), [bars]);
  const binOf = useMemo(() => new Map(h.bins.map((b) => [b.year, b])), [h]);
  const first = bars[0]?.year ?? 0;
  const last = bars.at(-1)?.year ?? first;

  const yearMax = useStore((s) => s.timeline.yearMax);
  const playing = useStore((s) => s.timeline.playing);
  const setTimeline = useStore((s) => s.setTimeline);

  const [hover, setHover] = useState<number | null>(null);
  const [dragging, setDragging] = useState(false);
  const plotRef = useRef<HTMLDivElement>(null);
  const width = useElementWidth(plotRef);

  /** The latest year the map is drawing. With no year chosen the map draws them all, so the bars stay lit. */
  const shown = yearMax ?? last;
  const ticks = useMemo(() => axisTicks(first, last, TICK_STEP), [first, last]);

  // Playback: step by elapsed time so the run takes PLAY_SECONDS whatever the frame rate; stop on the last year.
  useEffect(() => {
    if (!playing) return;
    const span = last - first;
    if (span <= 0 || motionOff()) {
      setTimeline({ yearMax: last, playing: false });
      return;
    }
    const held = useStore.getState().timeline.yearMax;
    const from = held === null || held >= last ? first : held;
    const ms = PLAY_SECONDS * 1000 * ((last - from) / span);
    const t0 = performance.now();
    let raf = requestAnimationFrame(function step(t) {
      const p = ms <= 0 ? 1 : Math.min(1, (t - t0) / ms);
      if (p >= 1) {
        setTimeline({ yearMax: last, playing: false });
        return;
      }
      setTimeline({ yearMax: Math.round(from + p * (last - from)) });
      raf = requestAnimationFrame(step);
    });
    setTimeline({ yearMax: from });
    return () => cancelAnimationFrame(raf);
  }, [playing, first, last, setTimeline]);

  const setYear = useCallback(
    (y: number) => setTimeline({ yearMax: clampYear(y, first, last), playing: false }),
    [setTimeline, first, last],
  );

  const yearAtEvent = (e: React.PointerEvent<HTMLDivElement>) => {
    const r = e.currentTarget.getBoundingClientRect();
    return yearAtX(e.clientX - r.left, r.width, first, last);
  };

  const onKeyDown = (e: React.KeyboardEvent<HTMLDivElement>) => {
    const step =
      e.key === "ArrowLeft" || e.key === "ArrowDown"
        ? -1
        : e.key === "ArrowRight" || e.key === "ArrowUp"
          ? 1
          : 0;
    if (step !== 0) setYear(shown + step);
    else if (e.key === "Home") setYear(first);
    else if (e.key === "End") setYear(last);
    else return;
    e.preventDefault();
  };

  const onPlay = () => {
    if (playing) setTimeline({ playing: false });
    else if (motionOff()) setTimeline({ yearMax: last, playing: false });
    else setTimeline({ playing: true });
  };

  const hoverBin = hover === null ? undefined : binOf.get(hover);
  const band = bars.length > 0 ? width / bars.length : 0;
  const playheadRight = yearFraction(shown, first, last) > 0.85;

  return (
    <>
      <header className="flex items-center gap-2">
        <h2 className="min-w-0 flex-1 truncate font-medium text-[13px] text-ink">
          {WORDING.timelineCaption}
        </h2>
        <button
          type="button"
          onClick={onPlay}
          aria-label={playing ? "Pause" : "Play the years in order"}
          title={
            motionOff() ? "Playback is off in reduced motion" : playing ? "Pause" : "Play the years in order"
          }
          className="rounded-lg p-1.5 text-ink-2 transition-colors hover:bg-white/[0.06] hover:text-ink"
        >
          {playing ? <Pause className="size-4" /> : <Play className="size-4" />}
        </button>
        <button
          type="button"
          onClick={() => setTimeline({ yearMax: null, playing: false })}
          disabled={yearMax === null}
          title="Reset: show every year, undated holes included"
          className={cn(
            "rounded-lg px-2 py-1 text-[12px] transition-colors",
            yearMax === null ? "text-ink-3" : "text-ink-2 hover:bg-white/[0.06] hover:text-ink",
          )}
        >
          All years
        </button>
        <button
          type="button"
          onClick={() => setTimeline({ open: false, yearMax: null, playing: false })}
          aria-label="Close the timeline"
          title="Close the timeline"
          className="rounded-lg p-1.5 text-ink-3 transition-colors hover:bg-white/[0.06] hover:text-ink"
        >
          <X className="size-4" />
        </button>
      </header>

      <div className="mt-1.5 flex flex-wrap items-center gap-x-3 gap-y-1 text-[11px] text-ink-3">
        <Key color="var(--color-src-compilation)" label="Compilation" />
        <Key color="var(--color-src-geods)" label="GeoDS" />
        <span>The two datasets overlap: a hole can appear in both, and is counted in both.</span>
      </div>

      <div
        ref={plotRef}
        role="slider"
        tabIndex={0}
        aria-label="Latest year shown on the map"
        aria-valuemin={first}
        aria-valuemax={last}
        aria-valuenow={shown}
        aria-valuetext={yearMax === null ? "All years" : undefined}
        onKeyDown={onKeyDown}
        onPointerDown={(e) => {
          e.currentTarget.setPointerCapture(e.pointerId);
          setDragging(true);
          const y = yearAtEvent(e);
          setHover(y);
          setYear(y);
        }}
        onPointerMove={(e) => {
          const y = yearAtEvent(e);
          setHover(y);
          if (dragging) setYear(y);
        }}
        onPointerUp={(e) => {
          if (e.currentTarget.hasPointerCapture(e.pointerId))
            e.currentTarget.releasePointerCapture(e.pointerId);
          setDragging(false);
        }}
        onPointerCancel={() => setDragging(false)}
        onPointerLeave={() => setHover(null)}
        className="relative mt-2 cursor-ew-resize touch-none select-none rounded-sm outline-none focus-visible:ring-1 focus-visible:ring-focus"
        style={{ height: PLOT_H }}
      >
        <svg width={width} height={PLOT_H} aria-hidden="true" className="block">
          {hover !== null ? (
            <rect
              x={(hover - first) * band}
              y={0}
              width={band}
              height={PLOT_H}
              fill="rgb(255 255 255 / 0.07)"
            />
          ) : null}
          {width > 0
            ? bars.map((b, i) => {
                const hc = barHeight(b.cmp, max);
                const hg = barHeight(b.gds, max);
                const w = Math.max(1, band - 1);
                return (
                  <g key={b.year} opacity={b.year > shown ? 0.25 : 1}>
                    <rect
                      x={i * band}
                      y={PLOT_H - hc - hg}
                      width={w}
                      height={hg}
                      fill="var(--color-src-geods)"
                    />
                    <rect
                      x={i * band}
                      y={PLOT_H - hc}
                      width={w}
                      height={hc}
                      fill="var(--color-src-compilation)"
                    />
                  </g>
                );
              })
            : null}
        </svg>
        {yearMax === null ? null : (
          <div
            className="pointer-events-none absolute top-0 bottom-0 w-px bg-ink/80"
            style={{ left: `${yearFraction(shown, first, last) * 100}%` }}
          >
            <span
              data-instrument="timeline-year"
              className={cn(
                "absolute top-0 rounded bg-panel/80 px-1 text-[11px] text-ink tabular",
                playheadRight ? "right-1.5" : "left-1.5",
              )}
            >
              {formatNumber(shown, "year")}
            </span>
          </div>
        )}
      </div>

      <div className="relative mt-1 h-4 border-line border-t">
        {ticks.map((y) => (
          <span
            key={y}
            data-axis
            className="absolute top-[3px] text-[10px] text-ink-3 tabular leading-none"
            style={{
              left: `${yearFraction(y, first, last) * 100}%`,
              transform: y === first ? "none" : y === last ? "translateX(-100%)" : "translateX(-50%)",
            }}
          >
            {formatNumber(y, "year")}
          </span>
        ))}
      </div>

      <div className="mt-2 flex h-5 items-center gap-2 text-[11.5px]">
        {hoverBin ? (
          <>
            <span data-instrument="timeline-hover" className="text-ink-2 tabular">
              {formatNumber(hoverBin.year, "year")}
            </span>
            <span className="text-ink-3">·</span>
            <span className="text-ink-3">
              <V id={hoverBin.cmp} className="text-ink-2" /> compilation
            </span>
            <span className="text-ink-3">
              <V id={hoverBin.gds} className="text-ink-2" /> GeoDS
            </span>
          </>
        ) : (
          <span className="text-ink-3">
            Drag across the bars, or use the arrow keys, to set the latest year shown on the map.
          </span>
        )}
      </div>

      <p className="mt-1 border-line border-t pt-2 text-[11px] text-ink-3 leading-relaxed">
        The timeline filters the two provincial datasets only; holes from the files read stay on the map.{" "}
        <V id={h.undated_cmp} className="text-ink-2" /> compilation collars and{" "}
        <V id={h.undated_gds} className="text-ink-2" /> GeoDS holes carry no drilling year, so they are hidden
        while a year is selected and come back under All years.
      </p>
    </>
  );
}

function Key({ color, label }: { color: string; label: string }) {
  return (
    <span className="flex items-center gap-1.5">
      <span className="size-2 rounded-[2px]" style={{ background: color }} aria-hidden="true" />
      {label}
    </span>
  );
}

/** Reduced motion, or motion=0 in the URL: playback jumps to the end rather than animating. */
function motionOff(): boolean {
  if (new URLSearchParams(window.location.search).get("motion") === "0") return true;
  return (
    typeof window.matchMedia === "function" && window.matchMedia("(prefers-reduced-motion: reduce)").matches
  );
}

/** Measured plot width, so the bars land on whole pixels instead of being stretched by the viewBox. */
function useElementWidth(ref: React.RefObject<HTMLElement | null>): number {
  const [w, setW] = useState(0);
  useLayoutEffect(() => {
    const el = ref.current;
    if (!el) return;
    const update = () => setW(el.clientWidth);
    update();
    if (typeof ResizeObserver === "undefined") return;
    const ro = new ResizeObserver(update);
    ro.observe(el);
    return () => ro.disconnect();
  }, [ref]);
  return w;
}
