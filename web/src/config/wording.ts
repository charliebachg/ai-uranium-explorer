/**
 * Strings the app repeats in more than one place. Tests assert these exact strings; change the test with the
 * string, never the rule it stands for.
 */
export const WORDING = {
  banner:
    "Hole locations from public reports and provincial compilations. Colour shows extraction status, not prospectivity.",
  coverageSuffix: "uranium-tagged files read",
  noAssays: "Assay values: none in provincial tables",
  notRead: "Not in the files read here",
  opening:
    "Public Saskatchewan data, read into a checked store and scored three ways beside a model of exploration effort. It makes no geological judgement, proposes no drill targets, and no geologist has checked it.",
  timelineCaption: "Where people drilled, by year",
} as const;

/**
 * The banner the cell workspace carries at the top. It comes first because every score below it is a
 * retrospective fit to public data, and the fold tests say how much of the ranking is exploration history.
 */
export const prospectBanner =
  "Retrospective scoring of public data. No geologist has seen this. The fold tests show how much of the ranking is explained by where people already drilled.";

/** Phrases that must never appear in app copy (scanned by tests/unit/wording.test.ts). */
export const FORBIDDEN_PHRASES = [
  "high potential",
  "drill target",
  "geologically validated",
  "saskatchewan archive",
  "prospective ground",
  "hot spot",
] as const;
