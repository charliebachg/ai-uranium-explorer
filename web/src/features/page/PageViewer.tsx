import {
  ChevronLeft,
  ChevronRight,
  ExternalLink,
  Maximize2,
  SearchX,
  X,
  ZoomIn,
  ZoomOut,
} from "lucide-react";
import { AnimatePresence, motion } from "motion/react";
import { useCallback, useEffect, useLayoutEffect, useMemo, useRef, useState } from "react";
import { Chip } from "@/components/ui/StatusMark";
import { V } from "@/components/values/V";
import type { BBox, PagesIndex, Report, Val } from "@/data/contract";
import { resolveValue } from "@/data/registry";
import { pageImageUrl } from "@/data/reports";
import { holeValueIds, usePages, useReport } from "@/features/report/useReport";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";

type View = { scale: number; tx: number; ty: number };

/** The page a value was read from, with its box highlighted and the verbatim quote, checks and lineage below. */
export function PageViewer() {
  const page = useStore((s) => s.page);
  const valueId = useStore((s) => s.value);
  const rail = useStore((s) => s.ui.rail);
  const tourActive = useStore((s) => s.tour.step >= 0);
  const file = page?.file ?? null;
  const { data: report } = useReport(file);
  const { data: pages } = usePages(file);

  return (
    <AnimatePresence>
      {page && report && pages ? (
        <motion.section
          key="page-viewer"
          initial={{ opacity: 0, y: 12, scale: 0.985 }}
          animate={{ opacity: 1, y: 0, scale: 1 }}
          exit={{ opacity: 0, y: 12, scale: 0.985 }}
          transition={{ type: "spring", stiffness: 380, damping: 34 }}
          className={cn(
            "glass-opaque pointer-events-auto fixed top-[92px] right-[472px] z-20 flex flex-col overflow-hidden rounded-2xl shadow-2xl shadow-black/60",
            rail ? "left-[316px]" : "left-[76px]",
            // the walkthrough HUD takes the bottom strip; the page keeps its quote and lineage visible above it
            tourActive ? "bottom-[188px]" : "bottom-4",
          )}
          data-strict="page-viewer"
          aria-label="Report page"
        >
          <Viewer report={report} pages={pages} pageNo={page.page} valueId={valueId} />
        </motion.section>
      ) : null}
    </AnimatePresence>
  );
}

function Viewer({
  report,
  pages,
  pageNo,
  valueId,
}: {
  report: Report;
  pages: PagesIndex;
  pageNo: number;
  valueId: string | null;
}) {
  const file = report.summary.file_num;
  const openValue = useStore((s) => s.openValue);
  const setPage = useStore((s) => s.setPage);
  const hole = useStore((s) => s.hole);
  const meta = pages.pages.find((p) => p.page === pageNo) ?? null;
  const value = valueId ? (resolveValue(valueId) ?? null) : null;
  const img = pageImageUrl(meta?.image ?? null);

  const pageValues = useMemo(
    () =>
      Object.values(report.values).filter(
        (v): v is Val & { lineage: NonNullable<Val["lineage"]> } =>
          v.kind === "extracted" && v.lineage?.page === pageNo,
      ),
    [report, pageNo],
  );

  const pageIdx = pages.pages.findIndex((p) => p.page === pageNo);
  const goPage = useCallback(
    (delta: number) => {
      const next = pages.pages[pageIdx + delta];
      if (next) {
        openValue(null);
        setPage({ file, page: next.page });
      }
    },
    [pages, pageIdx, file, openValue, setPage],
  );

  const holeObj = report.holes.find((h) => h.hole_id === hole) ?? null;
  const order = useMemo(
    () => (holeObj ? holeValueIds(holeObj) : pageValues.map((v) => v.id)),
    [holeObj, pageValues],
  );
  const stepValue = useCallback(
    (delta: number) => {
      if (!order.length) return;
      const i = valueId ? order.indexOf(valueId) : -1;
      const nextId = order[(i + delta + order.length) % order.length];
      const v = nextId ? resolveValue(nextId) : null;
      if (nextId && v?.lineage) openValue(nextId, { file, page: v.lineage.page });
    },
    [order, valueId, openValue, file],
  );

  const [view, setView] = useState<View>({ scale: 1, tx: 0, ty: 0 });
  const [fitScale, setFitScale] = useState(1);
  const viewport = useRef<HTMLDivElement>(null);
  const w = meta?.width_px ?? 1275;
  const h = meta?.height_px ?? 1650;

  const fitPage = useCallback(() => {
    const el = viewport.current;
    if (!el) return;
    const s = Math.min(el.clientWidth / w, el.clientHeight / h) * 0.96;
    setFitScale(s);
    setView({ scale: s, tx: (el.clientWidth - w * s) / 2, ty: (el.clientHeight - h * s) / 2 });
  }, [w, h]);

  const zoomTo = useCallback(
    (bbox: BBox) => {
      const el = viewport.current;
      if (!el) return;
      const [x0, y0, x1, y1] = bbox;
      const bw = Math.max((x1 - x0) * w, 20);
      const bh = Math.max((y1 - y0) * h, 14);
      // keep reading context around the value: the box takes about a quarter of the width, never more than 1.5x zoom
      const s = Math.max(Math.min((el.clientWidth * 0.26) / bw, (el.clientHeight * 0.12) / bh, 1.5), 0.35);
      const cx = ((x0 + x1) / 2) * w;
      const cy = ((y0 + y1) / 2) * h;
      setView({ scale: s, tx: el.clientWidth / 2 - cx * s, ty: el.clientHeight * 0.42 - cy * s });
    },
    [w, h],
  );

  useLayoutEffect(() => {
    if (value?.lineage?.bbox && value.lineage.page === pageNo) zoomTo(value.lineage.bbox);
    else fitPage();
  }, [value, pageNo, zoomTo, fitPage]);

  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest("input,textarea")) return;
      if (e.key === "j") stepValue(1);
      else if (e.key === "k") stepValue(-1);
      else if (e.key === "]") goPage(1);
      else if (e.key === "[") goPage(-1);
      else if (e.key === "0") fitPage();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [stepValue, goPage, fitPage]);

  // wheel zoom around the cursor, drag to pan
  const drag = useRef<{ x: number; y: number; tx: number; ty: number } | null>(null);
  const [dragging, setDragging] = useState(false);
  const onWheel = (e: React.WheelEvent) => {
    const el = viewport.current;
    if (!el) return;
    const rect = el.getBoundingClientRect();
    const mx = e.clientX - rect.left;
    const my = e.clientY - rect.top;
    const factor = Math.exp(-e.deltaY * 0.0015);
    setView((v) => {
      const s = Math.min(Math.max(v.scale * factor, fitScale * 0.5), 6);
      const k = s / v.scale;
      return { scale: s, tx: mx - (mx - v.tx) * k, ty: my - (my - v.ty) * k };
    });
  };

  const box = value?.lineage?.page === pageNo ? (value.lineage.bbox ?? null) : null;
  const tone = value?.status === "miss" ? "#ff5fa2" : value?.status === "flag" ? "#fab219" : "#e6f0ff";

  return (
    <>
      <header className="flex items-center gap-2 border-line border-b px-3 py-2">
        <div className="min-w-0">
          <div className="truncate text-[13px] text-ink" data-ident>
            File {file}
          </div>
          <div className="text-[11px] text-ink-3" data-chrome>
            Page {pageNo} · {meta?.kind.replaceAll("_", " ") ?? "page"}
          </div>
        </div>
        <div className="ml-3 flex items-center gap-0.5">
          <IconBtn label="Previous page ([)" onClick={() => goPage(-1)} disabled={pageIdx <= 0}>
            <ChevronLeft className="size-4" />
          </IconBtn>
          <IconBtn
            label="Next page (])"
            onClick={() => goPage(1)}
            disabled={pageIdx >= pages.pages.length - 1}
          >
            <ChevronRight className="size-4" />
          </IconBtn>
        </div>
        <div className="ml-auto flex items-center gap-0.5">
          <IconBtn
            label="Zoom out"
            onClick={() => setView((v) => ({ ...v, scale: Math.max(v.scale / 1.25, fitScale * 0.5) }))}
          >
            <ZoomOut className="size-4" />
          </IconBtn>
          <IconBtn
            label="Zoom in"
            onClick={() => setView((v) => ({ ...v, scale: Math.min(v.scale * 1.25, 6) }))}
          >
            <ZoomIn className="size-4" />
          </IconBtn>
          <IconBtn label="Fit page (0)" onClick={fitPage}>
            <Maximize2 className="size-4" />
          </IconBtn>
          <a
            href={`${report.summary.source_url}`}
            target="_blank"
            rel="noreferrer"
            className="ml-1 flex items-center gap-1 rounded-lg px-2 py-1.5 text-[11.5px] text-ink-3 hover:bg-white/[0.05] hover:text-ink-2"
            title="Open the province's file"
          >
            Province <ExternalLink className="size-3" />
          </a>
          <IconBtn label="Close page (Esc)" onClick={() => openValue(null)}>
            <X className="size-4" />
          </IconBtn>
        </div>
      </header>

      <div
        ref={viewport}
        className={cn(
          "relative min-h-0 flex-1 overflow-hidden bg-[#07090d]",
          dragging ? "cursor-grabbing" : "cursor-grab",
        )}
        onWheel={onWheel}
        onPointerDown={(e) => {
          if ((e.target as Element).closest("[data-box]")) return;
          drag.current = { x: e.clientX, y: e.clientY, tx: view.tx, ty: view.ty };
          setDragging(true);
          (e.currentTarget as HTMLElement).setPointerCapture(e.pointerId);
        }}
        onPointerMove={(e) => {
          const d = drag.current;
          if (!d) return;
          setView((v) => ({ ...v, tx: d.tx + e.clientX - d.x, ty: d.ty + e.clientY - d.y }));
        }}
        onPointerUp={() => {
          drag.current = null;
          setDragging(false);
        }}
      >
        <div
          className="absolute top-0 left-0 origin-top-left"
          style={{
            width: w,
            height: h,
            transform: `translate(${view.tx}px, ${view.ty}px) scale(${view.scale})`,
            transition: dragging ? "none" : "transform 600ms cubic-bezier(0.22, 1, 0.36, 1)",
          }}
        >
          {img ? (
            <img
              src={img}
              alt={`Page ${pageNo} of assessment file ${file}`}
              width={w}
              height={h}
              draggable={false}
              className="block select-none rounded-sm shadow-2xl"
              style={{ filter: "brightness(0.9)" }}
            />
          ) : (
            <PageProxy w={w} h={h} />
          )}
          <svg
            className="absolute inset-0"
            width={w}
            height={h}
            viewBox={`0 0 ${w} ${h}`}
            role="img"
            aria-label="Value boxes"
          >
            <title>Value boxes</title>
            {box ? (
              <motion.path
                key={`mask-${valueId}`}
                initial={{ opacity: 0 }}
                animate={{ opacity: 1 }}
                transition={{ duration: 0.35 }}
                d={`M0 0H${w}V${h}H0Z M${box[0] * w} ${box[1] * h}V${box[3] * h}H${box[2] * w}V${box[1] * h}Z`}
                fill="#000"
                fillOpacity={0.5}
                fillRule="evenodd"
              />
            ) : null}
            {pageValues.map((v) => {
              const b = v.lineage.bbox;
              if (!b || v.id === valueId) return null;
              return (
                // biome-ignore lint/a11y/noStaticElementInteractions: page boxes mirror the value list, which is the keyboard path
                <rect
                  key={v.id}
                  data-box={v.id}
                  x={b[0] * w}
                  y={b[1] * h}
                  width={(b[2] - b[0]) * w}
                  height={(b[3] - b[1]) * h}
                  rx={3}
                  fill="rgba(138,180,255,0.06)"
                  stroke="rgba(138,180,255,0.35)"
                  strokeWidth={1.2 / view.scale}
                  className="cursor-pointer hover:fill-[rgba(138,180,255,0.18)]"
                  onClick={() => openValue(v.id, { file, page: pageNo })}
                />
              );
            })}
            {box ? (
              <motion.rect
                key={`box-${valueId}`}
                x={box[0] * w}
                y={box[1] * h}
                width={(box[2] - box[0]) * w}
                height={(box[3] - box[1]) * h}
                rx={4}
                fill="none"
                stroke={tone}
                strokeWidth={2.4 / view.scale}
                initial={{ pathLength: 0, opacity: 0 }}
                animate={{ pathLength: 1, opacity: 1 }}
                transition={{ duration: 0.55, ease: [0.22, 1, 0.36, 1], delay: 0.2 }}
                style={{ filter: `drop-shadow(0 0 ${6 / view.scale}px ${tone})` }}
                data-selected-box
              />
            ) : null}
          </svg>
        </div>

        {value && !box && value.lineage?.page === pageNo ? (
          <div className="absolute top-3 left-1/2 flex -translate-x-1/2 items-center gap-2 rounded-full bg-st-flag/15 px-3 py-1.5 text-[12px] text-st-flag">
            <SearchX className="size-3.5" /> The quote was not located on this page: check it by eye
          </div>
        ) : null}
      </div>

      {value ? <Evidence value={value} /> : <PageFooterHint count={pageValues.length > 0} />}
    </>
  );
}

function Evidence({ value }: { value: Val }) {
  const l = value.lineage;
  if (!l) return null;
  return (
    <footer className="border-line border-t bg-black/20 px-4 py-3">
      <div className="flex flex-wrap items-start gap-4">
        <div className="min-w-[160px]">
          <div className="text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">Value as printed</div>
          <div className="mt-1 text-[20px] text-ink">
            <V id={value.id} emptyText="not printed" />
          </div>
          {l.unit_source && l.unit_source !== "cell" ? (
            <div className="mt-0.5 text-[11px] text-ink-3">
              unit source: {l.unit_source.replaceAll("_", " ")}
            </div>
          ) : null}
        </div>
        <div className="min-w-0 flex-1">
          <div className="text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">Verbatim quote</div>
          {l.quote ? (
            <blockquote
              className="mt-1 rounded-md bg-paper px-2.5 py-1.5 font-quote text-[13px] text-paper-ink shadow-inner"
              data-ident="quote"
            >
              {l.quote}
            </blockquote>
          ) : (
            <div className="mt-1 rounded-md border border-line border-dashed px-2.5 py-1.5 text-[12.5px] text-ink-3 italic">
              The page prints nothing in this cell, so there is nothing to quote.
            </div>
          )}
        </div>
      </div>
      <div className="mt-2.5 flex flex-wrap items-center gap-1.5">
        {l.validators.length ? (
          l.validators.map((v) => (
            <Chip
              key={v.id + (v.message ?? "")}
              tone={
                v.outcome === "pass"
                  ? "pass"
                  : v.outcome === "fail"
                    ? "miss"
                    : v.outcome === "flag"
                      ? "flag"
                      : "neutral"
              }
            >
              <span data-ident>{v.id}</span> <span data-source-text>{v.message ?? v.outcome}</span>
            </Chip>
          ))
        ) : (
          <Chip>no validator messages</Chip>
        )}
        {!l.quote_located ? <Chip tone="flag">quote not located</Chip> : null}
      </div>
      <div
        className="mt-2 truncate text-[11px] text-ink-3"
        data-ident="lineage"
        title={`sha256 ${l.file_sha256}`}
      >
        model {l.model} · prompt {l.prompt_version} · run {l.run_id} · file sha256{" "}
        {l.file_sha256.slice(0, 12)}
      </div>
    </footer>
  );
}

function PageFooterHint({ count }: { count: boolean }) {
  return (
    <footer className="border-line border-t bg-black/20 px-4 py-2.5 text-[12px] text-ink-3">
      {count
        ? "Click a highlighted box to see the value, its quote and its checks."
        : "No values were read from this page."}
      <span className="ml-2 text-ink-3/70">
        <kbd className="rounded bg-white/10 px-1">J</kbd> <kbd className="rounded bg-white/10 px-1">K</kbd>{" "}
        step values · <kbd className="rounded bg-white/10 px-1">[</kbd>{" "}
        <kbd className="rounded bg-white/10 px-1">]</kbd> pages
      </span>
    </footer>
  );
}

function PageProxy({ w, h }: { w: number; h: number }) {
  return (
    <div
      className="flex items-center justify-center rounded-sm border border-line bg-[#11161d] text-center"
      style={{ width: w, height: h }}
    >
      <div className="max-w-[60%] text-[28px] text-ink-3 leading-snug">
        Page image withheld: this report carries no named licence.
        <br />
        Boxes show where each value sits on the page.
      </div>
    </div>
  );
}

function IconBtn({
  label,
  onClick,
  disabled,
  children,
}: {
  label: string;
  onClick: () => void;
  disabled?: boolean;
  children: React.ReactNode;
}) {
  return (
    <button
      type="button"
      aria-label={label}
      title={label}
      onClick={onClick}
      disabled={disabled}
      className="rounded-lg p-1.5 text-ink-3 transition-colors hover:bg-white/[0.06] hover:text-ink disabled:opacity-30"
    >
      {children}
    </button>
  );
}
