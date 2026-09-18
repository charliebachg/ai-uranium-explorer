/**
 * Frozen strings from the demo spec (research/05-demo-scope.md, Tables 4.2 and section 5).
 * Tests assert these exact strings; change them only together with the spec.
 */
export const WORDING = {
  banner:
    "Hole locations from public reports and provincial compilations. Colour shows extraction status, not prospectivity.",
  coverageSuffix: "uranium-tagged files read",
  noAssays: "Assay values: none in provincial tables",
  notRead: "Not in the files read by this demo",
  opening:
    "This is a public-data engineering demo. It reads what old Saskatchewan assessment reports print and measures how often it reads them wrongly. It makes no geological judgement and proposes no drill targets. I am not a geologist.",
  timelineCaption: "Where people drilled, by year",
} as const;

/**
 * The banner the cell workspace carries at the top, verbatim. It is the first thing on the page because every
 * score below it is a retrospective fit to public data, and the fold tests underneath say how much of the
 * ranking is exploration history rather than rock.
 */
export const prospectBanner =
  "Retrospective scoring of public data. No geologist has seen this, and the fold tests show how much of the ranking is explained by where people already drilled.";

/** Phrases that must never appear in app copy (scanned by tests/unit/wording.test.ts). */
export const FORBIDDEN_PHRASES = [
  "high potential",
  "drill target",
  "geologically validated",
  "saskatchewan archive",
  "prospective ground",
  "hot spot",
] as const;
