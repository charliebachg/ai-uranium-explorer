import { useEffect, useState } from "react";
import { Route, Switch } from "wouter";
import type { Manifest } from "@/data/contract";
import { loadManifest } from "@/data/loader";
import { exposeRegistryForTests } from "@/data/registry";
import { AgentRail } from "@/features/agent/AgentRail";
import { CursorReadout } from "@/features/datum/CursorReadout";
import { DatumLens } from "@/features/datum/DatumLens";
import { ShiftFieldLegend } from "@/features/datum/ShiftFieldLegend";
import { EvalPage } from "@/features/eval/EvalPage";
import { BulkHolePanel } from "@/features/hole/BulkHolePanel";
import { HoverCard } from "@/features/hovercard/HoverCard";
import { Intro, introWanted } from "@/features/intro/Intro";
import { LayerPanel } from "@/features/layers/LayerPanel";
import { LimitsPage } from "@/features/limits/LimitsPage";
import { PageViewer } from "@/features/page/PageViewer";
import { CommandPalette } from "@/features/palette/CommandPalette";
import { ReadinessPage } from "@/features/prospect/ReadinessPage";
import { ScoreLegend } from "@/features/prospect/ScoreLegend";
import { ReportPanel } from "@/features/report/ReportPanel";
import { ReviewPage } from "@/features/review/ReviewPage";
import { AttributionDialog } from "@/features/shell/AttributionDialog";
import { ErrorBoundary } from "@/features/shell/ErrorBoundary";
import { HonestyBanner } from "@/features/shell/HonestyBanner";
import { ManifestContext } from "@/features/shell/ManifestContext";
import { MapErrorToast } from "@/features/shell/MapErrorToast";
import { TopBar } from "@/features/shell/TopBar";
import { Timeline } from "@/features/timeline/Timeline";
import { TimelineButton } from "@/features/timeline/TimelineButton";
import { Tour } from "@/features/tour/Tour";
import { MapView } from "@/map/MapView";
import { SpikeApp } from "@/spikes/SpikeApp";
import { useStore } from "@/state/store";
import { startUrlSync } from "@/state/url";

let urlSyncStarted = false;

export function App() {
  if (new URLSearchParams(window.location.search).has("spike")) return <SpikeApp />;
  return (
    <ErrorBoundary>
      <Switch>
        <Route path="/eval">
          <PageShell>
            <EvalPage />
          </PageShell>
        </Route>
        <Route path="/data">
          <PageShell>
            <ReadinessPage />
          </PageShell>
        </Route>
        <Route path="/limits">
          <PageShell>
            <LimitsPage />
          </PageShell>
        </Route>
        <Route path="/review">
          <PageShell>
            <ReviewPage />
          </PageShell>
        </Route>
        <Route>
          <UraniumExplorer />
        </Route>
      </Switch>
      {/* the walkthrough and the palette follow the visitor across every view */}
      <Tour />
      <CommandPalette />
      <GlobalKeys />
      <ThemeSync />
    </ErrorBoundary>
  );
}

/** The theme lives on the document element so CSS tokens and the map style can both read it. */
function ThemeSync() {
  const theme = useStore((s) => s.theme);
  useEffect(() => {
    document.documentElement.dataset.theme = theme;
  }, [theme]);
  return null;
}

/** Shortcuts that work on every view. Panel-specific keys (Esc, D, M) stay with the map. */
function GlobalKeys() {
  const setPalette = useStore((s) => s.setPalette);
  const setTour = useStore((s) => s.setTour);
  const setTimeline = useStore((s) => s.setTimeline);
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const typing = (e.target as HTMLElement).closest?.("input,textarea");
      if ((e.metaKey || e.ctrlKey) && e.key.toLowerCase() === "k") {
        e.preventDefault();
        setPalette(true);
        return;
      }
      if (typing || e.metaKey || e.ctrlKey || e.altKey) return;
      if (e.key === "g") setTour({ step: 0, startedAt: Date.now() });
      else if (e.key === "t") setTimeline({ open: !useStore.getState().timeline.open });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, [setPalette, setTour, setTimeline]);
  return null;
}

/** Eval, data readiness and limits are full-page views with the same top bar. */
function PageShell({ children }: { children: React.ReactNode }) {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  useEffect(() => {
    loadManifest().then(setManifest, () => undefined);
  }, []);
  return (
    <ManifestContext.Provider value={manifest}>
      <div className="flex h-full flex-col bg-ground">
        <div className="flex items-start gap-3 px-4 pt-4 pb-3">
          <TopBar ready={!!manifest} />
        </div>
        <div className="min-h-0 flex-1">{children}</div>
      </div>
    </ManifestContext.Provider>
  );
}

function UraniumExplorer() {
  const [manifest, setManifest] = useState<Manifest | null>(null);
  const [error, setError] = useState<string | null>(null);

  if (!urlSyncStarted) {
    urlSyncStarted = true;
    startUrlSync();
    exposeRegistryForTests();
    if (introWanted()) useStore.setState({ intro: true });
  }

  useEffect(() => {
    loadManifest().then(setManifest, (e: unknown) => setError(String(e)));
  }, []);

  // Esc walks back one step: value, hole, report, selection
  useEffect(() => {
    const onKey = (e: KeyboardEvent) => {
      const st = useStore.getState();
      if (st.ui.attribution || (e.target as HTMLElement).closest?.("input,textarea")) return;
      if (e.key === "Escape") st.back();
      else if (e.key === "d" || e.key === "D") st.setDatum({ lens: !st.datum.lens });
      else if (e.key === "m" || e.key === "M") st.setDatum({ misread: !st.datum.misread });
    };
    window.addEventListener("keydown", onKey);
    return () => window.removeEventListener("keydown", onKey);
  }, []);

  return (
    <ManifestContext.Provider value={manifest}>
      <div
        className="relative h-full w-full overflow-hidden"
        data-perf={new URLSearchParams(window.location.search).get("perf") ?? undefined}
      >
        <MapView />

        {/* one row, never wrapping: a wrapped banner would fall behind the left rail */}
        <div className="pointer-events-none absolute inset-x-4 top-4 z-20 flex items-start gap-3">
          <TopBar ready={!!manifest} />
          <div className="min-w-0 flex-1">
            <HonestyBanner />
          </div>
        </div>

        {/* one left-hand column: the rail shrinks so its basemap footer is never pushed under the legends */}
        <div className="pointer-events-none absolute top-[92px] bottom-6 left-4 z-20 flex flex-col items-start gap-2">
          {manifest ? <LayerPanel /> : null}
          <ScoreLegend />
          <ShiftFieldLegend />
          <CursorReadout />
        </div>

        {/* one right-hand column: the file being read, the hole being read, or the agent on a cell */}
        <div className="pointer-events-none absolute top-[92px] right-4 bottom-6 z-20 flex flex-col items-end gap-3">
          <ReportPanel />
          <BulkHolePanel />
          <AgentRail />
        </div>

        <PageViewer />

        <div className="pointer-events-none absolute inset-x-0 bottom-6 z-20 flex flex-col items-center gap-2">
          <Timeline />
          <TimelineButton />
        </div>

        <DatumLens />
        <HoverCard />
        <AttributionDialog />
        <Intro />
        <MapErrorToast dataError={error} />
      </div>
    </ManifestContext.Provider>
  );
}
