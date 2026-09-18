import type { Report } from "@/data/contract";
import { loadRecordedChat } from "@/data/loader";
import { resolveValue } from "@/data/registry";
import { loadReport, loadReportIndex } from "@/data/reports";

/**
 * The tour points at whatever the current export actually contains, rather than hard-coded ids that would rot
 * the next time the pipeline runs. Each target is the first thing in the export that fits the step's purpose;
 * if nothing fits, the step still runs and says what is missing.
 */

export type EvidenceTarget = {
  file: string;
  hole: string;
  valueId: string | null;
  page: number | null;
  /** how the collar was positioned, so the step can say whether the page or the province placed it */
  positionSource: string;
  datumPrinted: string | null;
};

export type FailureTarget = {
  file: string;
  valueId: string;
  page: number;
  check: string;
  message: string;
};

/** The cell the recorded conversation is about, so the walkthrough opens the one it has answers for. */
export type RecordedCellTarget = { cellId: string; lon: number; lat: number; turns: number };

export type TourTargets = {
  evidence: EvidenceTarget | null;
  failure: FailureTarget | null;
  district: { center: [number, number]; zoom: number } | null;
  recordedCell: RecordedCellTarget | null;
};

function firstEvidence(rep: Report): EvidenceTarget | null {
  const hole = rep.holes.find((h) => h.position?.source.startsWith("extracted"));
  if (!hole) return null;
  // the collar numbers are the ones printed on the page; prefer one whose quote was located
  const candidates = [
    hole.collar.easting,
    hole.collar.northing,
    hole.collar.lat,
    hole.collar.lon,
    hole.collar.total_depth,
  ];
  let valueId: string | null = null;
  let page: number | null = null;
  for (const id of candidates) {
    if (!id) continue;
    const lin = rep.values[id]?.lineage;
    if (lin?.quote_located) {
      valueId = id;
      page = lin.page;
      break;
    }
  }
  const datum = hole.collar.datum_printed ? resolveValue(hole.collar.datum_printed) : undefined;
  return {
    file: rep.summary.file_num,
    hole: hole.hole_id,
    valueId,
    page,
    positionSource: hole.position?.source ?? "none",
    datumPrinted: datum?.as_printed ?? null,
  };
}

function firstFailure(rep: Report): FailureTarget | null {
  let fallback: FailureTarget | null = null;
  for (const [id, val] of Object.entries(rep.values)) {
    const lin = val.lineage;
    if (!lin) continue;
    for (const outcome of lin.validators) {
      if (!outcome.class_a) continue;
      const hit: FailureTarget = {
        file: rep.summary.file_num,
        valueId: id,
        page: lin.page,
        check: outcome.id,
        message: outcome.message ?? "",
      };
      // V01 is the unit failure the demo was built to catch: feet on the page, metres in the table
      if (outcome.id === "V01") return hit;
      fallback = fallback ?? hit;
    }
  }
  return fallback;
}

let cached: Promise<TourTargets> | null = null;

export function resolveTourTargets(): Promise<TourTargets> {
  cached =
    cached ??
    (async () => {
      const out: TourTargets = { evidence: null, failure: null, district: null, recordedCell: null };
      try {
        const chat = await loadRecordedChat();
        if (chat.lon !== null && chat.lat !== null)
          out.recordedCell = {
            cellId: chat.cell_id,
            lon: chat.lon,
            lat: chat.lat,
            turns: chat.turns.length,
          };
      } catch {
        // no recording yet: the step still runs, it simply has nothing to replay
      }
      const index = await loadReportIndex();
      let anyFailure: FailureTarget | null = null;
      for (const summary of index.reports) {
        if (out.evidence && out.failure) break;
        let rep: Report;
        try {
          rep = await loadReport(summary.file_num);
        } catch {
          continue;
        }
        out.evidence = out.evidence ?? firstEvidence(rep);
        // keep looking across files for the unit failure; settle for another class-A only if none exists
        const found = firstFailure(rep);
        if (found) {
          anyFailure = anyFailure ?? found;
          if (found.check === "V01") out.failure = found;
        }
        if (!out.district && out.evidence?.file === summary.file_num) {
          out.district = { center: summary.centroid, zoom: 9.2 };
        }
      }
      out.failure = out.failure ?? anyFailure;
      const first = index.reports[0];
      if (!out.district && first) out.district = { center: first.centroid, zoom: 9.2 };
      return out;
    })();
  return cached;
}
