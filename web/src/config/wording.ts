/**
 * Strings the app repeats in more than one place. Tests assert these exact strings; change the test with the
 * string, never the rule it stands for.
 */
export const WORDING = {
  coverageSuffix: "uranium-tagged files read",
  noAssays: "Assay values: none in provincial tables",
  notRead: "Not in the files read here",
  opening:
    "Public Saskatchewan data, read into a checked store and scored three ways beside a model of exploration effort. It makes no geological judgement, proposes no drill targets, and no geologist has checked it.",
  timelineCaption: "Where people drilled, by year",
} as const;

/** Phrases that must never appear in app copy (scanned by tests/unit/wording.test.ts). */
export const FORBIDDEN_PHRASES = [
  "high potential",
  "drill target",
  "geologically validated",
  "saskatchewan archive",
  "prospective ground",
  "hot spot",
] as const;
