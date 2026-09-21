import { ChevronLeft, ChevronRight, Route as RouteIcon, X } from "lucide-react";
import { motion } from "motion/react";
import { useCallback, useEffect, useRef, useState, useSyncExternalStore } from "react";
import { useLocation } from "wouter";
import { V } from "@/components/values/V";
import { hasValue, registryVersion, subscribeRegistry } from "@/data/registry";
import { cn } from "@/lib/cn";
import { mapController } from "@/map/MapView";
import { useStore } from "@/state/store";
import { clockText, sayParts, TOUR_SECONDS, TOUR_STEPS } from "./steps";
import { resolveTourTargets, type TourTargets } from "./targets";

const EMPTY: TourTargets = { evidence: null, failure: null, district: null, recordedCell: null };

/**
 * A number the script quotes, which may not be loaded yet.
 *
 * The walkthrough can be started with `G` the moment the page appears, before the manifest has arrived, and
 * `<V>` deliberately throws on an id it does not know — that loudness is worth keeping everywhere else, but
 * here it took the whole app down behind the error boundary if a visitor was quick on the keyboard. So the
 * script waits for its value instead: an ellipsis, then the number, the moment the registry has it.
 */
function ScriptValue({ id }: { id: string }) {
  useSyncExternalStore(subscribeRegistry, registryVersion);
  if (!hasValue(id)) return <span className="text-ink-3">…</span>;
  return <V id={id} />;
}

/**
 * Guided walkthrough: the acceptance story's steps (`steps.ts`) with their camera work, their scripted line
 * and the clock. It never advances by itself. The time shown is an instrument reading of how long this step
 * has been open against the budget in the script, so a rehearsal can see where it is running long.
 */
export function Tour() {
  const step = useStore((s) => s.tour.step);
  const startedAt = useStore((s) => s.tour.startedAt);
  const setTour = useStore((s) => s.setTour);
  const [, navigate] = useLocation();
  const [targets, setTargets] = useState<TourTargets | null>(null);
  const [stepStarted, setStepStarted] = useState<number>(() => Date.now());
  const [now, setNow] = useState<number>(() => Date.now());
  const lastRun = useRef<number>(-1);

  const active = step >= 0;

  useEffect(() => {
    if (!active || targets) return;
    resolveTourTargets().then(setTargets, () =>
      setTargets({ evidence: null, failure: null, district: null, recordedCell: null }),
    );
  }, [active, targets]);

  // run the step's own setup once it is current and the targets are in
  useEffect(() => {
    if (!active || !targets || lastRun.current === step) return;
    lastRun.current = step;
    setStepStarted(Date.now());
    TOUR_STEPS[step]?.enter({ targets, navigate, map: mapController() });
  }, [active, targets, step, navigate]);

  useEffect(() => {
    if (!active) return;
    const t = window.setInterval(() => setNow(Date.now()), 500);
    return () => window.clearInterval(t);
  }, [active]);

  const exit = useCallback(() => {
    lastRun.current = -1;
    setTour({ step: -1, startedAt: null });
  }, [setTour]);

  const go = useCallback(
    (to: number) => {
      if (to < 0 || to >= TOUR_STEPS.length) return exit();
      setTour({ step: to });
    },
    [setTour, exit],
  );

  useEffect(() => {
    if (!active) return;
    const onKey = (e: KeyboardEvent) => {
      if ((e.target as HTMLElement).closest?.("input,textarea")) return;
      if (e.key === "ArrowRight") go(step + 1);
      else if (e.key === "ArrowLeft") go(step - 1);
      else if (e.key === "Escape") exit();
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [active, step, go, exit]);

  const current = active ? TOUR_STEPS[step] : undefined;
  if (!current) return null;

  const stepSeconds = (now - stepStarted) / 1000;
  const totalSeconds = startedAt ? (now - startedAt) / 1000 : 0;
  const over = stepSeconds > current.seconds;
  const progress = Math.min(1, stepSeconds / current.seconds);

  return (
    <div className="pointer-events-none fixed inset-x-0 bottom-6 z-40 flex justify-center px-4">
      {/* one card for the whole walkthrough: stepping only swaps its contents, so a fast ← → cannot strand it
          mid-transition */}
      <motion.section
        data-strict="tour"
        data-testid="tour"
        data-step={current.id}
        initial={{ opacity: 0, y: 16 }}
        animate={{ opacity: 1, y: 0 }}
        transition={{ duration: 0.3, ease: [0.22, 1, 0.36, 1] }}
        className="glass-opaque pointer-events-auto w-[min(780px,calc(100vw-2rem))] overflow-hidden rounded-2xl shadow-2xl shadow-black/50"
        aria-label="Guided walkthrough"
      >
        <div className="flex items-center gap-3 border-line border-b px-4 py-2">
          <RouteIcon className="size-3.5 text-ink-3" aria-hidden="true" />
          <span className="text-[11px] text-ink-3 uppercase tracking-[0.14em]">Walkthrough</span>
          <span className="text-[12px] text-ink">{current.screen}</span>
          <span className="tabular text-[11.5px] text-ink-3" data-chrome>
            step {step + 1}/{TOUR_STEPS.length}
          </span>
          <span className="ml-auto flex items-center gap-2 text-[11.5px]">
            <span
              className={cn("tabular", over ? "text-st-flag" : "text-ink-2")}
              data-instrument
              title="time on this step against the script's budget"
            >
              {clockText(stepSeconds)} / {clockText(current.seconds)}
            </span>
            <span className="text-ink-3">·</span>
            <span
              className="tabular text-ink-3"
              data-instrument
              title="elapsed since the walkthrough started"
            >
              {clockText(totalSeconds)} of {clockText(TOUR_SECONDS)}
            </span>
          </span>
        </div>

        {/* keyed: each step's line fades in, while the card itself stays where it is */}
        <div key={current.id} className="animate-[ue-step-in_320ms_var(--ease-out-expo)] px-4 py-3">
          <p className="text-[13.5px] text-ink leading-relaxed">
            <Say say={typeof current.say === "function" ? current.say(targets ?? EMPTY) : current.say} />
          </p>
          {current.doing ? <p className="mt-2 text-[12px] text-ink-3">{current.doing}</p> : null}
          {!targets ? <p className="mt-2 text-[11.5px] text-ink-3">Opening the exported report…</p> : null}
        </div>

        <div className="flex items-center gap-2 border-line border-t px-4 py-2">
          <button
            type="button"
            onClick={() => go(step - 1)}
            disabled={step === 0}
            className="flex items-center gap-1 rounded-lg px-2.5 py-1.5 text-[12px] text-ink-2 hover:bg-white/[0.05] disabled:opacity-35"
          >
            <ChevronLeft className="size-3.5" /> Back
          </button>
          <button
            type="button"
            onClick={() => go(step + 1)}
            className="flex items-center gap-1 rounded-lg bg-white/[0.08] px-3 py-1.5 text-[12px] text-ink hover:bg-white/[0.12]"
          >
            {step === TOUR_STEPS.length - 1 ? "Finish" : "Next"} <ChevronRight className="size-3.5" />
          </button>
          <span className="ml-1 text-[11px] text-ink-3" data-chrome>
            ← → to move, esc to leave
          </span>
          <button
            type="button"
            onClick={exit}
            aria-label="Leave the walkthrough"
            className="ml-auto rounded-lg p-1.5 text-ink-3 hover:bg-white/[0.05] hover:text-ink-2"
          >
            <X className="size-3.5" />
          </button>
        </div>

        <div className="h-[2px] bg-white/[0.06]">
          <div
            className={cn("h-full transition-[width] duration-500", over ? "bg-st-flag" : "bg-ink-3")}
            style={{ width: `${progress * 100}%` }}
          />
        </div>
      </motion.section>
    </div>
  );
}

function Say({ say }: { say: string }) {
  return (
    <>
      {sayParts(say).map((part, i) =>
        "text" in part ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: parts are positional slices of a fixed string
          <span key={i}>{part.text}</span>
        ) : (
          <ScriptValue key={part.valueId} id={part.valueId} />
        ),
      )}
    </>
  );
}
