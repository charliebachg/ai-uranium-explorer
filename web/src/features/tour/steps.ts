import { WORDING } from "@/config/wording";
import { DEFAULT_CAMERA, useStore } from "@/state/store";
import type { TourTargets } from "./targets";

/**
 * The walkthrough from research report 05 (Table 5.3), as data. Each step sets the app state it needs and
 * carries the line to say out loud; the HUD drives them and the e2e test walks the same list.
 *
 * Three departures from 05. The eval step reports run statistics rather than recall, because the run has no
 * gold labels yet. The script's single-value failure step is gone: it walked through one misread depth, which
 * is a reading-pipeline detail rather than anything this demo turns on. And a step was added for the agent,
 * which is the part the whole system exists to make trustworthy.
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
  /** budget in seconds from Table 5.3; the HUD shows time against it and never advances on its own */
  seconds: number;
  screen: string;
  /** spoken line; {value-id} is replaced by the stored value. A function when the line depends on the export */
  say: string | ((t: TourTargets) => string);
  /** what is happening on screen, shown under the line */
  doing?: string;
  enter: (ctx: TourCtx) => void;
};

const store = () => useStore.getState();

export const TOUR_STEPS: TourStep[] = [
  {
    id: "title",
    seconds: 30,
    screen: "Title",
    say: WORDING.opening,
    doing:
      "Getting data out of decades of fragmented sources is the step this tests, on files anyone can download.",
    enter: (ctx) => {
      ctx.navigate("/");
      const s = store();
      s.openReport(null);
      s.select(null);
      s.setTimeline({ open: false, yearMax: null, playing: false });
      s.setChatReplay(null);
      ctx.map?.flyToCamera(DEFAULT_CAMERA, { duration: 1400 });
    },
  },
  {
    id: "district",
    seconds: 45,
    screen: "One district",
    say: "The province publishes where holes were drilled and what the rock was, but no assay values: {m:compilation_collars} collars are on this map and not one of them carries a grade.",
    doing:
      "Click any provincial collar: length, azimuth, dip, lithology, and the assay line saying there is none.",
    enter: (ctx) => {
      ctx.navigate("/");
      const s = store();
      s.openReport(null);
      const d = ctx.targets.district;
      if (d) ctx.map?.flyToCamera({ center: d.center, zoom: d.zoom }, { duration: 1800 });
      window.setTimeout(() => ctx.map?.selectNearestBulk(), 2000);
    },
  },
  {
    id: "evidence",
    seconds: 75,
    screen: "Hole panel and page",
    say: "The grades are only inside the reports. Here is a hole read out of one: every value opens the page it came from, with the box, the verbatim quote, the model and the run that produced it.",
    doing:
      "The collar also carries its datum as printed and the named grid operation used to move it, so the shift is a stated number rather than an assumption.",
    enter: (ctx) => {
      ctx.navigate("/");
      const t = ctx.targets.evidence;
      if (!t) return;
      const s = store();
      s.openHole(t.file, t.hole);
      if (t.valueId && t.page) s.openValue(t.valueId, { file: t.file, page: t.page });
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
    seconds: 50,
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
    say: "No gold set has been labelled yet, so there is no accuracy figure and this page says so at the top. What it does report is how much was read, how much of it points back at a quote on the page, where a second reader disagrees, and what every check caught.",
    doing:
      "Turning these into accuracy takes hand-keyed pages, a row count taken before the model output is seen, and a held-out split scored once.",
    enter: (ctx) => {
      store().openValue(null);
      ctx.navigate("/eval");
    },
  },
  {
    id: "ask",
    seconds: 55,
    screen: "Asking the agent",
    say: "Now I ask the agent about one cell. It can only read the tools this evidence came from, and every number it states carries the value id it came from. An answer that cites a number the tools did not return is withheld rather than shown, and the objection is shown in its place.",
    doing:
      "This exchange is recorded rather than live, so the room is not waiting on a model call. The live version is the same panel with the local service running.",
    enter: (ctx) => {
      ctx.navigate("/");
      const s = store();
      s.openReport(null);
      s.openValue(null);
      s.toggleLayer("prospect", true);
      s.setScoreModel("criteria");
      s.toggleLayer("compilation", true);
      const cell = ctx.targets.recordedCell;
      if (!cell) return;
      s.select({ dataset: "cell", id: -1, props: { cid: cell.cellId }, lngLat: [cell.lon, cell.lat] });
      s.setChatReplay({ cellId: cell.cellId, turns: cell.turns });
      ctx.map?.flyToCamera({ center: [cell.lon, cell.lat], zoom: 7.6 }, { duration: 1400 });
    },
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
