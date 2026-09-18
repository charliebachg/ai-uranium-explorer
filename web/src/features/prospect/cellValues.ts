import type { Candidate, ValRegistry, ValueId } from "@/data/contract";
import { registerValues } from "@/data/registry";
import type { ScoreModel } from "@/state/store";

/**
 * Some numbers arrive from the service and the exported cell file as plain fields rather than value ids: a
 * criterion's weight, a model's known-share, a candidate's score, what a conversation cost. The page is not
 * allowed to print a bare number, so each one is registered here as a stat carrying the record it came from.
 * Nothing is computed: the number registered is the number the export or the service wrote.
 */

function add(
  reg: ValRegistry,
  id: string,
  value: number,
  fmt: "ratio3" | "int" | "m2",
  note: string,
  unit?: string,
): void {
  reg[id] = {
    id: id as ValueId,
    kind: "stat",
    as_printed: null,
    value,
    unit_as_printed: null,
    fmt,
    ...(unit ? { unit } : {}),
    note,
  };
}

// ---------- the exported cell file (what the map is drawn from) ----------

export function mapScoreId(cellId: string, model: ScoreModel): ValueId {
  return `c:map:${cellId}:${model}` as ValueId;
}

export function mapKnownShareId(cellId: string): ValueId {
  return `c:map:${cellId}:known_share` as ValueId;
}

/** The short property names the exported cell file uses, and the model each one belongs to. */
const SCORE_PROP: Record<ScoreModel, string> = { criteria: "c", learned: "l", effort: "e", difference: "d" };

/**
 * The selected cell's own row of the exported file, registered straight off the map feature. The point is that
 * the map keeps saying what it is showing when the local service is not running: these four numbers are all the
 * static build knows about a cell, and they are the ones the export wrote, not ones computed here.
 */
export function registerMapCell(cellId: string, props: Record<string, unknown>): void {
  const reg: ValRegistry = {};
  const source = `from prospect/scores.geojson, the exported row for cell ${cellId}`;
  const numberAt = (key: string): number | null =>
    typeof props[key] === "number" ? (props[key] as number) : null;
  for (const model of ["criteria", "learned", "effort", "difference"] as const) {
    const value = numberAt(SCORE_PROP[model]);
    if (value === null) continue;
    const note =
      model === "difference" ? `learned score minus effort score, ${source}` : `${model} score ${source}`;
    add(reg, mapScoreId(cellId, model), value, "ratio3", note);
  }
  const known = numberAt("k");
  if (known !== null) {
    add(
      reg,
      mapKnownShareId(cellId),
      known,
      "ratio3",
      `share of this cell's labelled neighbourhood that is already known, ${source}`,
    );
  }
  if (Object.keys(reg).length) registerValues(reg);
}

// ---------- the service ----------

export function knownShareId(cellId: string, model: string): ValueId {
  return `c:known:${cellId}:${model}` as ValueId;
}

export function weightId(cellId: string, criterion: string): ValueId {
  return `c:weight:${cellId}:${criterion}` as ValueId;
}

export function candidateScoreId(cellId: string): ValueId {
  return `c:cand:${cellId}` as ValueId;
}

export function turnCostId(conversationId: string, turn: number): ValueId {
  return `c:chat:${conversationId}:${turn}` as ValueId;
}

export function conversationCostId(conversationId: string): ValueId {
  return `c:chat:${conversationId}:total` as ValueId;
}

/** known_share as the cell_scores tool reported it, one per model row. */
export function registerKnownShares(cellId: string, rows: Record<string, unknown>[]): void {
  const reg: ValRegistry = {};
  for (const row of rows) {
    const model = typeof row.model === "string" ? row.model : null;
    const value = typeof row.known_share === "number" ? row.known_share : null;
    if (model === null || value === null) continue;
    add(
      reg,
      knownShareId(cellId, model),
      value,
      "ratio3",
      `known_share for the ${model} model at cell ${cellId}, as the cell_scores tool returned it`,
    );
  }
  if (Object.keys(reg).length) registerValues(reg, { notify: false });
}

/** Each criterion's weight in the handbook, as the criteria_breakdown tool returned it. */
export function registerCriterionWeights(cellId: string, rows: Record<string, unknown>[]): void {
  const reg: ValRegistry = {};
  for (const row of rows) {
    const key = typeof row.criterion === "string" ? row.criterion : null;
    const weight = typeof row.weight === "number" ? row.weight : null;
    if (key === null || weight === null) continue;
    add(
      reg,
      weightId(cellId, key),
      weight,
      "m2",
      `weight the handbook gives ${key}, as the criteria_breakdown tool returned it`,
    );
  }
  if (Object.keys(reg).length) registerValues(reg, { notify: false });
}

export function registerCandidateScores(rows: Candidate[]): void {
  const reg: ValRegistry = {};
  for (const row of rows) {
    add(
      reg,
      candidateScoreId(row.cell_id),
      row.score,
      "ratio3",
      `criteria score for cell ${row.cell_id}, as /api/cells ranked it`,
    );
  }
  if (Object.keys(reg).length) registerValues(reg, { notify: false });
}

/** What one answer cost, and what the conversation has cost so far, as the service reported them. */
export function registerChatCost(
  conversationId: string,
  turn: number,
  turnCost: number | undefined,
  total: number | undefined,
): void {
  const reg: ValRegistry = {};
  if (typeof turnCost === "number") {
    add(reg, turnCostId(conversationId, turn), turnCost, "m2", "list-price cost of this answer", "USD");
  }
  if (typeof total === "number") {
    add(
      reg,
      conversationCostId(conversationId),
      total,
      "m2",
      "list-price cost of this conversation so far",
      "USD",
    );
  }
  if (Object.keys(reg).length) registerValues(reg, { notify: false });
}
