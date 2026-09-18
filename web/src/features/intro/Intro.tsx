import { AnimatePresence, motion } from "motion/react";
import { useEffect, useRef, useState } from "react";
import { V } from "@/components/values/V";
import { WORDING } from "@/config/wording";
import { mapController } from "@/map/MapView";
import { DEFAULT_CAMERA, useStore } from "@/state/store";

/**
 * Opening sequence: the globe holds behind the title card, then the camera flies down to the province and three
 * stored counters settle in. The counters are <V> values, never a count-up animation: a rolling odometer would
 * put numbers on screen that are not what the data says.
 */

const GLOBE_START = { center: [-96, 46] as [number, number], zoom: 2.2, bearing: 0, pitch: 0 };
const SEEN_KEY = "lr:intro-seen";

export function reducedMotion(): boolean {
  if (typeof window === "undefined") return true;
  if (new URLSearchParams(window.location.search).get("motion") === "0") return true;
  return window.matchMedia("(prefers-reduced-motion: reduce)").matches;
}

/**
 * The intro runs on a plain visit to the map. Any query string means the visitor arrived by a deep link (or a
 * test) and wants the state in the URL, not a flight; `?intro=1` forces it anyway.
 */
export function introWanted(): boolean {
  if (typeof window === "undefined") return false;
  const q = new URLSearchParams(window.location.search);
  if (q.get("intro") === "1") return true;
  if (q.get("intro") === "0" || [...q.keys()].length > 0) return false;
  if (reducedMotion()) return false;
  try {
    return window.sessionStorage.getItem(SEEN_KEY) !== "1";
  } catch {
    return true;
  }
}

function markSeen() {
  try {
    window.sessionStorage.setItem(SEEN_KEY, "1");
  } catch {
    // private browsing: the intro simply runs again next time
  }
}

type Phase = "title" | "flying" | "counters";

export function Intro() {
  const active = useStore((s) => s.intro);
  const setIntro = useStore((s) => s.setIntro);
  const [phase, setPhase] = useState<Phase>("title");
  const timers = useRef<number[]>([]);
  const beginRef = useRef<HTMLButtonElement>(null);

  // hold the globe behind the title card, with the keyboard already on "Begin"
  useEffect(() => {
    if (!active) return;
    mapController()?.map.jumpTo(GLOBE_START);
    beginRef.current?.focus();
  }, [active]);

  useEffect(
    () => () => {
      for (const t of timers.current) window.clearTimeout(t);
    },
    [],
  );

  if (!active) return null;

  const finish = () => {
    for (const t of timers.current) window.clearTimeout(t);
    timers.current = [];
    markSeen();
    setIntro(false);
    mapController()?.flyToCamera(DEFAULT_CAMERA, { instant: true });
  };

  const begin = () => {
    markSeen();
    setPhase("flying");
    const ctl = mapController();
    const flight = ctl
      ? ctl.introFlight({ center: DEFAULT_CAMERA.center, zoom: DEFAULT_CAMERA.zoom })
      : Promise.resolve();
    void flight.then(() => setPhase("counters"));
    // the flight resolves on moveend; a fallback keeps the sequence moving if the camera is interrupted
    timers.current.push(window.setTimeout(() => setPhase("counters"), 5200));
    timers.current.push(window.setTimeout(finish, 9600));
  };

  return (
    <div
      data-strict="intro"
      data-testid="intro"
      // the phase is what the sequence turns on; without it a stuck intro is indistinguishable from a slow one
      data-phase={phase}
      className="absolute inset-0 z-40 flex items-center justify-center"
      style={{ pointerEvents: phase === "title" ? "auto" : "none" }}
    >
      <AnimatePresence>
        {phase === "title" ? (
          <motion.div
            key="title"
            initial={{ opacity: 0 }}
            animate={{ opacity: 1 }}
            exit={{ opacity: 0 }}
            transition={{ duration: 0.5 }}
            className="absolute inset-0 flex items-center justify-center bg-ground/72 backdrop-blur-[2px]"
          >
            <motion.div
              initial={{ opacity: 0, y: 14 }}
              animate={{ opacity: 1, y: 0 }}
              transition={{ duration: 0.7, ease: [0.22, 1, 0.36, 1] }}
              className="mx-6 max-w-[640px] text-center"
            >
              <div className="text-[11px] text-ink-3 uppercase tracking-[0.28em]">
                Public Saskatchewan uranium assessment files
              </div>
              <h1 className="mt-3 font-semibold text-[42px] text-ink leading-none tracking-tight">
                AI Uranium Explorer
              </h1>
              <p className="mx-auto mt-5 max-w-[56ch] text-[13.5px] text-ink-2 leading-relaxed">
                {WORDING.opening}
              </p>
              <div className="mt-7 flex items-center justify-center gap-3">
                <button
                  type="button"
                  ref={beginRef}
                  onClick={begin}
                  className="rounded-xl bg-ink px-5 py-2.5 font-medium text-[13px] text-ground transition-transform hover:scale-[1.02]"
                >
                  Begin
                </button>
                <button
                  type="button"
                  onClick={finish}
                  className="rounded-xl px-4 py-2.5 text-[13px] text-ink-3 hover:text-ink-2"
                >
                  Skip to the map
                </button>
              </div>
              <div className="mt-6 text-[11.5px] text-ink-3">
                No ranking of ground, no scores. Colour shows extraction status, not prospectivity.
              </div>
            </motion.div>
          </motion.div>
        ) : null}
      </AnimatePresence>

      <AnimatePresence>
        {phase === "counters" ? (
          <motion.div
            key="counters"
            initial={{ opacity: 0, y: 10 }}
            animate={{ opacity: 1, y: 0 }}
            exit={{ opacity: 0, y: -6 }}
            transition={{ duration: 0.6, ease: [0.22, 1, 0.36, 1] }}
            className="glass absolute bottom-[86px] flex items-stretch gap-6 rounded-2xl px-6 py-4 shadow-2xl shadow-black/50"
          >
            <Counter delay={0} id="m:compilation_collars" label="provincial collars on the map" />
            <div className="w-px bg-line" />
            <Counter delay={0.12} id="m:uranium_files" label="uranium-tagged assessment files" />
            <div className="w-px bg-line" />
            <Counter delay={0.24} id="m:files_read" label="read, page by page, by this demo" />
          </motion.div>
        ) : null}
      </AnimatePresence>
    </div>
  );
}

/** The number settles into place; it is the stored value the whole time. */
function Counter({ id, label, delay }: { id: string; label: string; delay: number }) {
  return (
    <motion.div
      initial={{ opacity: 0, y: 12, filter: "blur(6px)" }}
      animate={{ opacity: 1, y: 0, filter: "blur(0px)" }}
      transition={{ duration: 0.75, delay, ease: [0.22, 1, 0.36, 1] }}
      className="min-w-[132px]"
    >
      <div className="text-[26px] text-ink leading-none">
        <V id={id} />
      </div>
      <div className="mt-1.5 max-w-[150px] text-[11px] text-ink-3 leading-snug">{label}</div>
    </motion.div>
  );
}
