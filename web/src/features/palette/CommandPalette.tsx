import { Command } from "cmdk";
import {
  Compass,
  Crosshair,
  Eraser,
  FileText,
  Image,
  Layers,
  LocateFixed,
  Map as MapIcon,
  MapPinOff,
  Move,
  Route,
  Scale,
  Search,
  Clock as Timeline,
} from "lucide-react";
import { Fragment, useCallback, useEffect, useMemo, useRef } from "react";
import { useLocation } from "wouter";
import { StatusMark } from "@/components/ui/StatusMark";
import { useReportIndex } from "@/features/report/useReport";
import { cn } from "@/lib/cn";
import { mapController } from "@/map/MapView";
import { type Camera, DEFAULT_CAMERA, useStore } from "@/state/store";
import { buildItems, PALETTE_GROUPS, type PaletteItem, type Part, type ToolId } from "./items";

/**
 * ⌘K palette. The parent mounts it always and owns the keybinding; this renders only while `palette` is open.
 *
 * Highlighting a report or a hole peeks at it on the map after a short debounce. A peek is camera-only: it never
 * opens the report, and dismissing the palette without choosing anything flies back to the camera captured when
 * the palette opened, so browsing the list cannot quietly move the map out from under the user.
 */

const PEEK_MS = 200;

export function CommandPalette() {
  const open = useStore((s) => s.palette);
  return open ? <Palette /> : null;
}

function Palette() {
  const setPalette = useStore((s) => s.setPalette);
  const visible = useStore((s) => s.visible);
  const basemap = useStore((s) => s.basemap);
  const datum = useStore((s) => s.datum);
  const timelineOpen = useStore((s) => s.timeline.open);
  const [, navigate] = useLocation();
  const index = useReportIndex().data;

  const inputRef = useRef<HTMLInputElement>(null);
  const cameraAtOpen = useRef<Camera | null>(null);
  const peeked = useRef(false);
  const peekTimer = useRef<number | null>(null);

  const items = useMemo(
    () => buildItems({ index, visible, basemap, datum, timelineOpen }),
    [index, visible, basemap, datum, timelineOpen],
  );
  const byValue = useMemo(() => new Map(items.map((i) => [i.value, i])), [items]);

  // focus the input on open, hand focus back to the document body on close, and remember where the map was
  useEffect(() => {
    inputRef.current?.focus();
    cameraAtOpen.current = readCamera();
    return () => {
      if (peekTimer.current !== null) window.clearTimeout(peekTimer.current);
      if (document.activeElement instanceof HTMLElement) document.activeElement.blur();
    };
  }, []);

  const close = useCallback(
    (restoreCamera: boolean) => {
      if (peekTimer.current !== null) window.clearTimeout(peekTimer.current);
      if (restoreCamera && peeked.current && cameraAtOpen.current) {
        mapController()?.flyToCamera(cameraAtOpen.current, { duration: 900 });
      }
      setPalette(false);
    },
    [setPalette],
  );

  const peek = useCallback(
    (value: string) => {
      if (peekTimer.current !== null) window.clearTimeout(peekTimer.current);
      if (stillness()) return;
      const action = byValue.get(value)?.action;
      if (!action || (action.kind !== "report" && action.kind !== "hole")) return;
      if (action.kind === "hole" && !action.lonlat) return;
      peekTimer.current = window.setTimeout(() => {
        const ctl = mapController();
        if (!ctl) return;
        peeked.current = true;
        if (action.kind === "report") ctl.fitReport(action.file);
        else if (action.lonlat) ctl.flyTo(action.lonlat, 12);
      }, PEEK_MS);
    },
    [byValue],
  );

  const run = useCallback(
    (item: PaletteItem) => {
      const st = useStore.getState();
      const a = item.action;
      if (a.kind === "go") navigate(a.href);
      else if (a.kind === "report") st.openReport(a.file);
      else if (a.kind === "hole") st.openHole(a.file, a.hole);
      else if (a.kind === "layer") st.toggleLayer(a.layer);
      else if (a.kind === "basemap") st.setBasemap(a.basemap);
      else runTool(a.tool);
      close(false);
    },
    [close, navigate],
  );

  return (
    <div
      className="fixed inset-0 z-50 flex items-start justify-center px-4 pt-[12vh]"
      data-strict="palette"
      data-testid="palette"
    >
      <button
        type="button"
        aria-label="Close the command palette"
        tabIndex={-1}
        onClick={() => close(true)}
        className="absolute inset-0 cursor-default bg-black/50 backdrop-blur-[2px]"
      />
      <div
        role="dialog"
        aria-modal="true"
        aria-label="Command palette"
        className="glass relative w-[620px] max-w-full overflow-hidden rounded-2xl shadow-2xl shadow-black/60"
        onKeyDown={(e) => {
          // the app's own Esc ladder must not also fire: this dismissal is the palette's
          if (e.key !== "Escape") return;
          e.stopPropagation();
          close(true);
        }}
      >
        <Command label="Command palette" loop onValueChange={peek}>
          <div className="flex items-center gap-2.5 border-line border-b px-4">
            <Search className="size-4 shrink-0 text-ink-3" aria-hidden="true" />
            <Command.Input
              ref={inputRef}
              placeholder="Search reports, holes, layers and commands"
              aria-label="Search reports, holes, layers and commands"
              className="h-12 w-full bg-transparent text-[14px] text-ink outline-none placeholder:text-ink-3"
            />
          </div>

          <Command.List className="max-h-[52vh] overflow-y-auto p-2">
            <Command.Empty className="px-3 py-8 text-center text-[12.5px] text-ink-3">
              Nothing here matches that.
            </Command.Empty>
            {PALETTE_GROUPS.map((group) => {
              const rows = items.filter((i) => i.group === group);
              if (!rows.length) return null;
              return (
                <Command.Group
                  key={group}
                  heading={group}
                  className="mb-1 [&_[cmdk-group-heading]]:px-2 [&_[cmdk-group-heading]]:pt-2 [&_[cmdk-group-heading]]:pb-1 [&_[cmdk-group-heading]]:text-[10.5px] [&_[cmdk-group-heading]]:text-ink-3 [&_[cmdk-group-heading]]:uppercase [&_[cmdk-group-heading]]:tracking-[0.12em]"
                >
                  {rows.map((item) => (
                    <Row key={item.id} item={item} onRun={run} />
                  ))}
                </Command.Group>
              );
            })}
          </Command.List>

          <footer className="flex items-center gap-4 border-line border-t px-4 py-2.5 text-[11px] text-ink-3">
            <Hint keys="↑↓" text="to move" />
            <Hint keys="↵" text="to open" />
            <Hint keys="esc" text="to close" />
          </footer>
        </Command>
      </div>
    </div>
  );
}

function Row({ item, onRun }: { item: PaletteItem; onRun: (item: PaletteItem) => void }) {
  return (
    <Command.Item
      value={item.value}
      onSelect={() => onRun(item)}
      className="flex cursor-pointer items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors duration-150 data-[selected=true]:bg-white/[0.06]"
    >
      <span
        className={cn(
          "flex size-6 shrink-0 items-center justify-center rounded-md",
          item.on ? "bg-src-compilation/25 text-st-pass" : "bg-black/30 text-ink-3",
        )}
      >
        <Glyph item={item} />
      </span>
      <span className="min-w-0 flex-1">
        <Line parts={item.title} sep=" " className="block truncate text-[13px] text-ink" />
        {item.detail.length ? (
          <Line parts={item.detail} sep=" · " className="block truncate text-[11px] text-ink-3" />
        ) : null}
      </span>
      {item.placed === false ? (
        <span className="flex shrink-0 items-center gap-1 text-[11px] text-ink-3">
          <MapPinOff className="size-3" aria-hidden="true" /> not placed
        </span>
      ) : null}
      {item.kbd ? (
        <kbd className="shrink-0 rounded bg-white/10 px-1 text-[10px] text-ink-3" data-chrome>
          {item.kbd}
        </kbd>
      ) : null}
    </Command.Item>
  );
}

function Glyph({ item }: { item: PaletteItem }) {
  const a = item.action;
  if (a.kind === "hole" && item.status) return <StatusMark status={item.status} size={11} />;
  const cls = "size-3.5";
  if (a.kind === "go") return <Compass className={cls} aria-hidden="true" />;
  if (a.kind === "report") return <FileText className={cls} aria-hidden="true" />;
  if (a.kind === "hole") return <MapIcon className={cls} aria-hidden="true" />;
  if (a.kind === "layer") return <Layers className={cls} aria-hidden="true" />;
  if (a.kind === "basemap") return <Image className={cls} aria-hidden="true" />;
  return <ToolGlyph tool={a.tool} className={cls} />;
}

function ToolGlyph({ tool, className }: { tool: ToolId; className: string }) {
  const props = { className, "aria-hidden": true } as const;
  if (tool === "lens") return <Crosshair {...props} />;
  if (tool === "misread") return <Move {...props} />;
  if (tool === "timeline") return <Timeline {...props} />;
  if (tool === "tour") return <Route {...props} />;
  if (tool === "attribution") return <Scale {...props} />;
  if (tool === "reset") return <LocateFixed {...props} />;
  return <Eraser {...props} />;
}

/** Renders a row line, marking identifier spans so the strict-numbers walk lets their digits through. */
function Line({ parts, sep, className }: { parts: Part[]; sep: string; className?: string }) {
  return (
    <span className={className}>
      {parts.map((p, i) => (
        <Fragment key={p.text}>
          {i > 0 ? sep : null}
          {p.ident ? <span data-ident>{p.text}</span> : p.text}
        </Fragment>
      ))}
    </span>
  );
}

function Hint({ keys, text }: { keys: string; text: string }) {
  return (
    <span className="flex items-center gap-1.5" data-chrome>
      <kbd className="rounded bg-white/[0.07] px-1.5 py-0.5 text-[10px] text-ink-2">{keys}</kbd>
      {text}
    </span>
  );
}

function runTool(tool: ToolId): void {
  const st = useStore.getState();
  if (tool === "lens") st.setDatum({ lens: !st.datum.lens });
  else if (tool === "misread") st.setDatum({ misread: !st.datum.misread });
  else if (tool === "timeline") st.setTimeline({ open: !st.timeline.open });
  else if (tool === "tour") st.setTour({ step: 0, startedAt: Date.now() });
  else if (tool === "attribution") st.setUi({ attribution: true });
  else if (tool === "reset") mapController()?.flyToCamera(DEFAULT_CAMERA);
  else st.select(null);
}

function readCamera(): Camera | null {
  const map = mapController()?.map;
  if (!map) return null;
  const c = map.getCenter();
  return { center: [c.lng, c.lat], zoom: map.getZoom(), bearing: map.getBearing(), pitch: map.getPitch() };
}

/** No map peeking when the URL asks for stillness or the reader prefers reduced motion. */
function stillness(): boolean {
  if (new URLSearchParams(window.location.search).get("motion") === "0") return true;
  return window.matchMedia?.("(prefers-reduced-motion: reduce)").matches ?? false;
}
