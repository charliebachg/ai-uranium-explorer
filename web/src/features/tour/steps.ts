import { WORDING } from "@/config/wording";
import { DEFAULT_CAMERA, useStore } from "@/state/store";
import type { TourTargets } from "./targets";

/**
 * The walkthrough, as data. Each step sets the app state it needs and carries its own line; the HUD drives
 * them and the e2e test walks the same list.
 *
 * The order is the acceptance story: the data and its readiness, the scores against the null they must beat,
 * the model results, the analyst's chains on an enabled cell, then the agent on that cell. The exchange with
 * the agent is prerecorded (`ue prospect record`), so no step waits on a model call.
 */

export type TourCtx = {
  targets: TourTargets;
  navigate: (to: string) => void;
  map: {
    flyToCamera: (
      cam: { center: [number, number]; zoom: number; bearing?: number; pitch?: number },
      opts?: { duration?: number; instant?: boolean },
    ) => void;
    selectNearestBulk: () => unknown;
  } | null;
};

export type TourStep = {
  id: string;
  /** budget in seconds; the HUD shows time against it and never advances on its own */
  seconds: number;
  screen: string;
  /** spoken line; {value-id} is replaced by the stored value. A function when the line depends on the export */
  say: string | ((t: TourTargets) => string);
  /** what is happening on screen, shown under the line */
  doing?: string;
  enter: (ctx: TourCtx) => void;
};

const store = () => useStore.getState();

/** The camera over the enabled cell the recording is about, with the rail open beside it. */
const CELL_ZOOM = 7.6;

/**
 * Open the recorded cell in the rail. The rail keeps its own tab; it switches itself to the conversation
 * when a replay is set, and the tour asks for the evidence tab back the way a person would, by clicking it,
 * because nothing else reaches that state from here.
 */
function openRecordedCell(ctx: TourCtx, replayTurns: number | null): void {
  ctx.navigate("/");
  const s = store();
  s.openReport(null);
  s.openValue(null);
  s.toggleLayer("prospect", true);
  s.setScoreModel("criteria");
  const cell = ctx.targets.recordedCell;
  if (!cell) {
    s.setChatReplay(null);
    return;
  }
  s.select({ dataset: "cell", id: -1, props: { cid: cell.cellId }, lngLat: [cell.lon, cell.lat] });
  s.setChatReplay(replayTurns === null ? null : { cellId: cell.cellId, turns: replayTurns });
  ctx.map?.flyToCamera({ center: [cell.lon, cell.lat], zoom: CELL_ZOOM }, { duration: 1400 });
  if (replayTurns === null) {
    window.setTimeout(() => {
      document
        .querySelector<HTMLButtonElement>('[data-testid="tab-evidence"][aria-selected="false"]')
        ?.click();
    }, 250);
  }
}

export const TOUR_STEPS: TourStep[] = [
  {
    id: "title",
    seconds: 30,
    screen: "Title",
    say: WORDING.opening,
    doing:
      "One system on public files, covering the data, the scores, the model results, the analyst and the agent.",
    enter: (ctx) => {
      ctx.navigate("/");
      const s = store();
      s.openReport(null);
      s.openValue(null);
      s.select(null);
      s.setTimeline({ open: false, yearMax: null, playing: false });
      s.setChatReplay(null);
      ctx.map?.flyToCamera(DEFAULT_CAMERA, { duration: 1400 });
    },
  },
  {
    id: "data",
    seconds: 45,
    screen: "Data and readiness",
    say: "Everything here is public. The page gives each feature's coverage of the grid, which features are thin, and which geophysics is not public.",
    doing:
      "The readiness gate asks five things of every dataset, and no agent phase starts until every row is green.",
    enter: (ctx) => {
      const s = store();
      s.openValue(null);
      s.setChatReplay(null);
      ctx.navigate("/data");
    },
  },
  {
    id: "map",
    seconds: 45,
    screen: "The map",
    say: "One district, with conductors, faults, the graphitic host, lake geochemistry and drill collars switched on. The cell open in the rail was read and its chain computed in advance.",
    doing: "Each layer loads only when it is switched on, grouped by what it is evidence of.",
    enter: (ctx) => {
      ctx.navigate("/");
      const s = store();
      s.openReport(null);
      s.openValue(null);
      s.setChatReplay(null);
      s.toggleLayer("prospect", false);
      for (const id of ["conductors", "faults", "host", "lakeSediment", "compilation"] as const)
        s.toggleLayer(id, true);
      const cell = ctx.targets.recordedCell;
      if (!cell) return;
      s.select({ dataset: "cell", id: -1, props: { cid: cell.cellId }, lngLat: [cell.lon, cell.lat] });
      ctx.map?.flyToCamera({ center: [cell.lon, cell.lat], zoom: 8.6 }, { duration: 1800 });
    },
  },
  {
    id: "scores",
    seconds: 45,
    screen: "Scoring the ground",
    say: "The analysis cells, coloured by the knowledge-driven criteria score. Every threshold and weight is written down beside its source, and none is fitted to the answer.",
    doing: "Only this layer colours by a value, and a cell nobody could score is drawn as a ring.",
    enter: (ctx) => {
      ctx.navigate("/");
      const s = store();
      s.openReport(null);
      s.openValue(null);
      s.setChatReplay(null);
      s.select(null);
      s.toggleLayer("prospect", true);
      s.toggleLayer("compilation", false);
      s.setScoreModel("criteria");
      ctx.map?.flyToCamera({ center: [-105.2, 57.9], zoom: 7 }, { duration: 1400 });
    },
  },
  {
    id: "nullmodel",
    seconds: 45,
    screen: "The null model",
    say: "The same ground, now showing the learned score minus the exploration-effort score. Blue is where geology beats drilling history, and most of the basin is not blue.",
    doing: "The effort model sees drillhole counts, survey footprints and first-drilled years, and no rock.",
    enter: (ctx) => {
      ctx.navigate("/");
      const s = store();
      s.toggleLayer("prospect", true);
      s.setScoreModel("difference");
      ctx.map?.flyToCamera({ center: [-105.2, 57.9], zoom: 7 }, { duration: 1200 });
    },
  },
  {
    id: "eval",
    seconds: 60,
    screen: "Eval",
    say: "The Eval page. The reading run statistics sit on top, then the model search, the dated hindcast and the analyst benchmark.",
    doing:
      "A candidate is validated only if its interval lies wholly above the null's, and the code applies that rule.",
    enter: (ctx) => {
      const s = store();
      s.openValue(null);
      s.setChatReplay(null);
      ctx.navigate("/eval");
    },
  },
  {
    id: "chains",
    seconds: 50,
    screen: "The analyst's chains",
    say: "Back on the enabled cell, the evidence tab. Under the scores sits the stored analyst chain, one node per criterion, each citing the values it read.",
    doing:
      "The verdict tops out at supports a closer look, and a node that failed its gate is shown withheld.",
    enter: (ctx) => openRecordedCell(ctx, null),
  },
  {
    id: "ask",
    seconds: 60,
    screen: "Asking the agent",
    say: "The agent answers about this cell, and each question is routed before any tool runs. Every number it states is a value id, and one without a citation is withheld.",
    doing: "This exchange was recorded on the cheap model, so nothing waits on a model call.",
    enter: (ctx) => openRecordedCell(ctx, ctx.targets.recordedCell?.askTurns ?? 0),
  },
  {
    id: "analyst",
    seconds: 50,
    screen: "Calling the analyst",
    say: (t) => {
      const cell = t.recordedCell;
      const made = !cell?.chainId
        ? ""
        : cell.chainPublished
          ? ", and the new chain was published into the store"
          : ", and the verifier withheld its chain";
      return `The last question hands the cell to the analyst itself. The job card shows each stage, the verdict, the cost, and a diff against the stored chain${made}.`;
    },
    doing:
      "The agent finds, explains and invokes, but recording a geologist's insight is the one action it does not take.",
    enter: (ctx) => openRecordedCell(ctx, ctx.targets.recordedCell?.turns ?? 0),
  },
  {
    id: "limits",
    seconds: 40,
    screen: "Limits",
    say: "Trusting this for drilling needs geologists to define which errors matter and label a sample. That is the first ask.",
    doing: "Each row gives a claim, whether public data supports it, and what it would take.",
    enter: (ctx) => {
      const s = store();
      s.openValue(null);
      s.setChatReplay(null);
      ctx.navigate("/limits");
    },
  },
];

export const TOUR_SECONDS = TOUR_STEPS.reduce((n, s) => n + s.seconds, 0);

/**
 * When each step starts, derived from the budgets above rather than written beside them. Hand-written clocks
 * drift the moment a step is added or re-timed, and a script that disagrees with its own running order is
 * worse than one with no clock at all.
 */
export const TOUR_CLOCKS: string[] = TOUR_STEPS.map((_step, i) =>
  clockText(TOUR_STEPS.slice(0, i).reduce((n, s) => n + s.seconds, 0)),
);

/** "0:00" style clock for the HUD; the digits are an instrument readout, not a stored value. */
export function clockText(seconds: number): string {
  const s = Math.max(0, Math.floor(seconds));
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, "0")}`;
}

/** Split a scripted line into text and {value-id} tokens. */
export function sayParts(say: string): ({ text: string } | { valueId: string })[] {
  const out: ({ text: string } | { valueId: string })[] = [];
  let i = 0;
  const re = /\{([^}]+)\}/g;
  for (let m = re.exec(say); m; m = re.exec(say)) {
    if (m.index > i) out.push({ text: say.slice(i, m.index) });
    out.push({ valueId: m[1] as string });
    i = m.index + m[0].length;
  }
  if (i < say.length) out.push({ text: say.slice(i) });
  return out;
}
