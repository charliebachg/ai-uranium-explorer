import type { LayerSpecification, StyleSpecification } from "maplibre-gl";
import type { BasemapDef } from "@/config/basemaps";
import { GLOBAL_STATE_DEFAULTS, type LayerGroup, type LayerGroupId, type Slot } from "@/config/layers";

const SLOT_ORDER: Slot[] = ["relief", "context", "data", "top"];

/**
 * The globe below zoom 3.5 morphing to mercator by 5.5: the intro flies in from the globe and every later view
 * (zoom 5.4 and in) is plain mercator, so no measurement is ever read off a curved projection.
 */
const GLOBE = {
  projection: {
    type: ["interpolate", ["linear"], ["zoom"], 3.5, "vertical-perspective", 5.5, "mercator"],
  },
  sky: {
    "sky-color": "#0b0f14",
    "horizon-color": "#1e2c3d",
    "fog-color": "#0b0f14",
    "fog-ground-blend": 0.6,
    "atmosphere-blend": ["interpolate", ["linear"], ["zoom"], 2, 0.9, 4, 0.5, 5.5, 0],
  },
} as unknown as Partial<StyleSpecification>;

export interface ComposeInput {
  base: StyleSpecification;
  basemap: BasemapDef;
  groups: LayerGroup[];
  visible: Record<LayerGroupId, boolean>;
  globalState?: Record<string, unknown>;
}

/**
 * Pure: basemap style + app layer groups -> one complete style. App layers are injected before each slot's
 * anchor layer, so basemap labels stay above data. Visibility is baked into layout so setStyle(diff) is cheap.
 */
export function composeStyle({
  base,
  basemap,
  groups,
  visible,
  globalState,
}: ComposeInput): StyleSpecification {
  const bySlot = new Map<Slot, LayerSpecification[]>();
  const sources = { ...base.sources };
  for (const g of groups) {
    for (const [id, src] of Object.entries(g.sources)) {
      if (id in sources && sources[id] !== src) throw new Error(`source id collision: ${id}`);
      sources[id] = src;
    }
    const on = visible[g.id] ?? g.defaultVisible;
    const layers = g.layers.map(
      (l) =>
        ({
          ...l,
          layout: { ...("layout" in l ? l.layout : {}), visibility: on ? "visible" : "none" },
          metadata: { ...((l as { metadata?: object }).metadata ?? {}), "lr:group": g.id },
        }) as LayerSpecification,
    );
    bySlot.set(g.slot, [...(bySlot.get(g.slot) ?? []), ...layers]);
  }

  const anchorOf = (slot: Slot): string | null => basemap.anchors[slot] || null;
  const placed = new Set<Slot>();
  const out: LayerSpecification[] = [];
  const emit = (slot: Slot) => {
    if (placed.has(slot)) return;
    placed.add(slot);
    out.push(...(bySlot.get(slot) ?? []));
  };

  for (const layer of base.layers) {
    for (const slot of SLOT_ORDER) {
      if (!placed.has(slot) && anchorOf(slot) === layer.id) {
        // keep slot order: anything earlier that has not been placed goes first
        for (const earlier of SLOT_ORDER.slice(0, SLOT_ORDER.indexOf(slot))) emit(earlier);
        emit(slot);
      }
    }
    out.push(layer);
  }
  for (const slot of SLOT_ORDER) emit(slot);

  const ids = new Set<string>();
  for (const l of out) {
    if (ids.has(l.id)) throw new Error(`duplicate layer id ${l.id}`);
    ids.add(l.id);
  }

  const state: Record<string, { default: unknown }> = {};
  for (const [k, v] of Object.entries({ ...GLOBAL_STATE_DEFAULTS, ...(globalState ?? {}) }))
    state[k] = { default: v };

  return { ...base, sources, layers: out, state, ...GLOBE } as StyleSpecification;
}

/**
 * Honesty checks on a composed style; returns human-readable violations (empty means clean).
 *
 * The rule is that colour encodes a data source or an extraction status, never a value — because a map that
 * paints ground by score is read as a map of where to drill, whatever the caption says.
 *
 * There is exactly one exception, and it is declared rather than assumed: a layer carrying
 * `metadata["lr:score"]` may colour by the score fields, because showing the three scores side by side is the
 * argument the demo is making. The exception is written into this check, not into a silence: a layer that
 * colours by a computed key without declaring itself is a violation, and a layer that declares itself but
 * colours by something other than a score field is a violation too.
 */
export const SCORE_FIELDS = new Set(["c", "l", "e", "d", "scoreKey"]);

export function honestyViolations(style: StyleSpecification): string[] {
  const problems: string[] = [];
  const allowedColorInputs = new Set(["status", "src"]);
  const colorProps = /(-color|color-relief-color)$/;
  const walk = (expr: unknown, onGet: (field: string | null) => void) => {
    if (!Array.isArray(expr)) return;
    if (expr[0] === "get" || expr[0] === "global-state") onGet(typeof expr[1] === "string" ? expr[1] : null);
    for (const e of expr) walk(e, onGet);
  };
  for (const l of style.layers) {
    if (l.type === "heatmap") problems.push(`${l.id}: heatmap layers are not allowed`);
    const meta = (l.metadata ?? {}) as Record<string, unknown>;
    const declaresScore = meta["lr:score"] === true;
    const paint = ("paint" in l ? l.paint : undefined) as Record<string, unknown> | undefined;
    for (const [prop, val] of Object.entries(paint ?? {})) {
      if (!colorProps.test(prop)) continue;
      walk(val, (field) => {
        if (declaresScore) {
          // the exception is narrow: a declared score layer may read score fields and nothing else
          if (field !== null && !SCORE_FIELDS.has(field))
            problems.push(`${l.id}.${prop}: score layer colours by "${field}", which is not a score`);
          return;
        }
        problems.push(
          field === null
            ? `${l.id}.${prop}: colour driven by a computed field without declaring metadata["lr:score"]`
            : allowedColorInputs.has(field)
              ? ""
              : `${l.id}.${prop}: colour driven by data field "${field}"`,
        );
      });
    }
    const layout = ("layout" in l ? l.layout : undefined) as Record<string, unknown> | undefined;
    const tf = layout?.["text-field"];
    if (tf !== undefined && (l.metadata as Record<string, unknown> | undefined)?.["lr:group"]) {
      walk(tf, (field) => {
        if (field && /^(y|len|td|az|dip|inc|value|grade|shift|offset)$/.test(field))
          problems.push(`${l.id}: numeric field "${field}" rendered as canvas text (use a <V> marker)`);
      });
    }
  }
  return problems.filter(Boolean);
}
