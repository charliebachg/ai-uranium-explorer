import { Route, Search } from "lucide-react";
import { Link, useLocation } from "wouter";
import { V } from "@/components/values/V";
import { WORDING } from "@/config/wording";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";
import { FixtureBadge } from "./FixtureWatermark";
import { KeyButton } from "./KeyDialog";

export function TopBar({ ready }: { ready: boolean }) {
  return (
    <div className="glass pointer-events-auto flex items-center gap-4 rounded-2xl py-2.5 pr-3 pl-3.5 shadow-2xl shadow-black/40">
      <div className="flex items-center gap-2.5">
        <Mark />
        <div className="leading-tight">
          <div className="font-semibold text-[15px] tracking-tight">AI Uranium Explorer</div>
          <div className="text-[11px] text-ink-3">Public Saskatchewan uranium assessment files</div>
        </div>
      </div>
      <div className="h-8 w-px bg-line" />
      <div
        className="flex items-baseline gap-1.5 rounded-lg bg-white/[0.04] px-2.5 py-1.5 text-[12px]"
        data-testid="coverage"
      >
        {ready ? (
          <>
            <V id="m:files_read" className="font-medium text-ink" />
            <span className="text-ink-3">of</span>
            <V id="m:uranium_files" className="text-ink-2" />
            <span className="text-ink-3">{WORDING.coverageSuffix}</span>
          </>
        ) : (
          <span className="h-3 w-40 animate-pulse rounded bg-white/10" />
        )}
      </div>
      <Nav />
      <Actions />
      <FixtureBadge />
    </div>
  );
}

/** Search, the walkthrough and the API key, reachable from every view (the first two are also ⌘K and G). */
function Actions() {
  const setPalette = useStore((s) => s.setPalette);
  const setTour = useStore((s) => s.setTour);
  const tourActive = useStore((s) => s.tour.step >= 0);
  return (
    <div className="flex items-center gap-1.5">
      <button
        type="button"
        onClick={() => setPalette(true)}
        className="flex items-center gap-2 rounded-lg bg-black/25 px-2.5 py-1.5 text-[12px] text-ink-3 transition-colors hover:text-ink-2"
        aria-label="Search reports, holes, layers and commands"
      >
        <Search className="size-3.5" aria-hidden="true" />
        <span className="hidden lg:inline">Search</span>
        <kbd className="rounded bg-white/[0.07] px-1.5 py-0.5 text-[10px]" data-chrome>
          ⌘K
        </kbd>
      </button>
      <button
        type="button"
        onClick={() => setTour({ step: tourActive ? -1 : 0, startedAt: tourActive ? null : Date.now() })}
        className={cn(
          "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12px] transition-colors",
          tourActive ? "bg-raised text-ink" : "bg-black/25 text-ink-3 hover:text-ink-2",
        )}
        aria-pressed={tourActive}
      >
        <Route className="size-3.5" aria-hidden="true" />
        <span className="hidden lg:inline">{tourActive ? "Leave tour" : "Tour"}</span>
      </button>
      <KeyButton />
    </div>
  );
}

function Mark() {
  return (
    <svg width="26" height="26" viewBox="0 0 26 26" aria-hidden="true">
      <defs>
        <linearGradient id="ue-mark" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#e6f0ff" />
          <stop offset="1" stopColor="#3987e5" />
        </linearGradient>
      </defs>
      <rect x="1" y="1" width="24" height="24" rx="7" fill="#161c25" stroke="rgba(255,255,255,0.1)" />
      <path
        d="M8 7v12h10"
        fill="none"
        stroke="url(#ue-mark)"
        strokeWidth="2"
        strokeLinecap="round"
        strokeLinejoin="round"
      />
      <circle cx="13.5" cy="11" r="2.1" fill="none" stroke="#e6f0ff" strokeWidth="1.4" />
    </svg>
  );
}

const LINKS: [string, string][] = [
  ["/", "Map"],
  ["/data", "Data"],
  ["/eval", "Eval"],
  ["/limits", "Limits"],
  ["/review", "Review"],
];

function Nav() {
  const [location] = useLocation();
  return (
    <nav className="flex items-center gap-0.5 rounded-lg bg-black/25 p-1" aria-label="Views">
      {LINKS.map(([href, label]) => {
        const active = href === "/" ? location === "/" : location.startsWith(href);
        return (
          <Link
            key={href}
            href={href}
            className={cn(
              "rounded-md px-2.5 py-1.5 text-[12px] transition-colors",
              active ? "bg-raised text-ink" : "text-ink-3 hover:text-ink-2",
            )}
          >
            {label}
          </Link>
        );
      })}
    </nav>
  );
}
