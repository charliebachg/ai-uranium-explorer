import { WORDING } from "@/config/wording";
import { DEFAULT_CAMERA, useStore } from "@/state/store";
import type { TourTargets } from "./targets";

/**
 * The walkthrough, as data. Each step sets the app state it needs and carries the line to say out loud; the
 * HUD drives them and the e2e test walks the same list.
 *
 * It tells the prototype's acceptance story: a person opens the dashboard, sees the data and how ready it
 * is, reads the scores against the null they must beat, inspects the model results on the Eval page and the
 * analyst's chains on an enabled cell, talks to the interface agent about that cell, and has it call the
 * analyst. The exchange with the agent is prerecorded (`ue prospect record`), so the room never waits on a
 * model, and the steps say so out loud. The reading pipeline that the earlier tour walked through is now one
 * line on the Eval page rather than three steps of its own.
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
      "One system on public files: the data and how ready it is, three scores and the null they must beat, the model results, an analyst that argues from the record, and an agent that can only answer from it.",
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
    say: "Everything here is public. This page says what the grid actually covers, feature by feature: how much of the basin each one reaches, which are thin, and the geophysics that is not public at all. It is coverage, not a ranking of ground.",
    doing:
      "The readiness gate at the bottom asks five things of every dataset: in the store, licensed for its use, coverage stated, servable, and versioned to a hashed pull. No agent phase starts until every row is green.",
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
    say: "One district, with the evidence layers on: mapped conductors, faults, the graphitic host, lake geochemistry, and the collars of where people already drilled. The cell open in the rail is one of the enabled cells: its assessment files were read and its analyst chain was computed in advance.",
    doing:
      "Every layer loads only when it is switched on. The rail groups them by what they are evidence of, and names the grids this ground does not publish.",
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
    say: "Now the analysis cells, coloured by the knowledge-driven criteria score: conductor proximity, a mapped graphitic host, fault proximity, the unconformity depth window, lake geochemistry. Every threshold and weight is written down beside the paper it came from, and none of it is fitted to the answer.",
    doing:
      "This is the one layer allowed to colour by a value, so the banner changes to say so and a legend appears. A cell nobody could score is drawn as a ring, not as a low score.",
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
    say: "Same ground, now showing the learned score minus the exploration-effort score. Blue is where a model trained on geology beats a model that knows nothing but where people have already drilled. Most of the basin is not blue.",
    doing:
      "The effort model sees drillhole counts, survey footprints and first-drilled years. No rock at all. It exists to be beaten, and on this grid it is not.",
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
    say: "The Eval page. At the top, the reading pipeline's run statistics, and why they are not accuracy. Below them the score models: the model search, every candidate one tracked run scored out of fold against the null; the dated hindcast, labels and drilling frozen at a cutoff year and each later discovery reported as the share of the basin that scored at least as well; and the analyst benchmark, one row per arm on the same open cells, with the staged loop's columns per chain.",
    doing:
      "A candidate is validated only if its interval lies wholly above the null's, and the code applies that rule rather than a reader of the table. A cheaper stack sits beside the strong one on the same cells.",
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
    say: "Back on the enabled cell, the evidence tab. Under the scores and what each criterion contributed sits the chain the staged analyst computed offline: one node per criterion, each citing the values it read; a verifier that read the whole chain and sent faulty nodes back, round by round; and a verdict that tops out at supports a closer look.",
    doing:
      "The chain is stored, so it loads from the local service with no model call. A node or a decision that failed its gate is shown withheld, with the objection beside it.",
    enter: (ctx) => openRecordedCell(ctx, null),
  },
  {
    id: "ask",
    seconds: 60,
    screen: "Asking the agent",
    say: "Now I ask the agent about this cell. Each question is routed first, and the line under the answer says what kind it was read as and which tools its plan fetched. Every number it states is a value id from those tools, shown as a chip; an answer citing a number they did not return is withheld, objection in its place. The question it must not answer, a grade, is declined with its reason.",
    doing:
      "This exchange was recorded on the cheap model, so the room never waits on a model call. The live panel is the same, with the local service running.",
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
          ? ", and its chain was published into the store"
          : ", and the verifier withheld its chain, which is a result too";
      return `The last question hands the cell to the analyst itself. The agent submits a job; the card shows the stages as the staged loop closed them, then the verdict and what it cost. The next turn reports the diff: the new chain's node statuses and verdict beside the stored chain's. This job ran for real when the session was recorded${made}.`;
    },
    doing:
      "The agent adds no signal of its own: it finds, explains and invokes. Recording a geologist's insight is the one action the tour does not take, because a recording has no geologist in it.",
    enter: (ctx) => openRecordedCell(ctx, ctx.targets.recordedCell?.turns ?? 0),
  },
  {
    id: "limits",
    seconds: 40,
    screen: "Limits",
    say: "To trust this for drilling, your geologists would define which errors matter and label a sample. That is my first ask.",
    doing: "One line per row: what public data can support, what it cannot, and what each would take.",
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
