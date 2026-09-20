import { z } from "zod";

/**
 * The data contract for everything under web/public/data. The pipeline's export_web.py writes these shapes and
 * `npm run validate:data` checks them before the app ever loads them.
 *
 * Rule 1: no bare numbers. Every numeric value shown to a user is a ValueId pointing at a Val record, except
 * geometry coordinates, page boxes, array indices and the datum-grid arrays.
 * Rule 2: the browser does no evidential maths. Offsets, shifts, misread positions, feet-to-metre geometry,
 * counts and metrics all arrive from the pipeline as Vals.
 */

export const SCHEMA_VERSION = "1.0.0";

// ---------- identifiers ----------

/**
 * Namespaces: x extracted, d derived, p provincial lithology, s bulk source property, m manifest stat,
 * e eval metric, c cell and coverage value, g datum grid node, h histogram bin. Report-scoped ids carry the
 * file number as the second
 * segment (x:64L04-0075:9f2c1a7b) so a deep link can lazy-load the report.
 */
export const ValueId = z
  .string()
  .regex(/^(x|d|p|s|m|e|c|g|h):[^\s]+$/, "value id must be namespaced, e.g. x:<file>:<hash>")
  .brand<"ValueId">();
export type ValueId = z.infer<typeof ValueId>;

export const BBox = z
  .tuple([z.number(), z.number(), z.number(), z.number()])
  .refine(
    ([x0, y0, x1, y1]) => x0 >= 0 && y0 >= 0 && x1 <= 1 && y1 <= 1 && x1 >= x0 && y1 >= y0,
    "bbox is normalised [x0,y0,x1,y1] on the upright page image",
  );
export type BBox = z.infer<typeof BBox>;

export const LonLat = z.tuple([z.number().min(-180).max(180), z.number().min(-90).max(90)]);
export type LonLat = z.infer<typeof LonLat>;

export const Status = z.enum(["pass", "flag", "miss", "corrected"]);
export type Status = z.infer<typeof Status>;

// ---------- values ----------

export const ValidatorOutcome = z.object({
  id: z.string(),
  outcome: z.enum(["pass", "flag", "fail", "na"]),
  severity: z.enum(["info", "warn", "error"]).optional(),
  class_a: z.boolean().optional(),
  message: z.string().optional(),
});
export type ValidatorOutcome = z.infer<typeof ValidatorOutcome>;

export const Lineage = z.object({
  file_num: z.string(),
  file_sha256: z.string().length(64),
  page: z.number().int().min(1),
  bbox: BBox.nullable(),
  quote: z.string(),
  quote_located: z.boolean(),
  unit_source: z
    .enum(["cell", "column_header", "table_title", "page_note", "prev_page_header", "not_printed"])
    .optional(),
  model: z.string(),
  prompt_version: z.string(),
  run_id: z.string(),
  extracted_at: z.string(),
  validators: z.array(ValidatorOutcome),
});
export type Lineage = z.infer<typeof Lineage>;

export const Derivation = z.object({
  op: z.string(), // e.g. ft_to_m, identity, geodesic_offset, ntv2_shift, count, recall, clopper_pearson_upper95
  inputs: z.array(ValueId),
  tool: z.string(), // e.g. "pyproj 3.8.0 / PROJ 9.8.1", "legacy_reader 0.1.0"
  params: z.record(z.string(), z.unknown()).optional(),
});

export const SourceRef = z.object({
  dataset: z.enum(["compilation", "geods", "geods_lith", "file_index", "deposits", "assessment_info"]),
  record_id: z.union([z.string(), z.number()]),
  field: z.string(),
  retrieved_at: z.string(),
});

export const Fmt = z.enum(["int", "year", "m1", "m2", "deg1", "deg5", "pct1", "ratio3", "text"]);
export type Fmt = z.infer<typeof Fmt>;

export const Val = z
  .object({
    id: ValueId,
    kind: z.enum(["extracted", "derived", "source", "stat", "metric"]),
    as_printed: z.string().nullable(), // exact printed tokens; extracted values display this
    value: z.union([z.number(), z.string()]).nullable(), // parsed, in the printed unit, never converted
    unit_as_printed: z.string().nullable(),
    fmt: Fmt.optional(), // formatter for non-extracted kinds
    unit: z.string().optional(), // display unit for non-extracted kinds (m, %, deg)
    status: Status.optional(),
    lineage: Lineage.optional(),
    derivation: Derivation.optional(),
    source: SourceRef.optional(),
    note: z.string().optional(),
  })
  .superRefine((v, ctx) => {
    if (v.kind === "extracted" && !v.lineage)
      ctx.addIssue({ code: "custom", message: `extracted value ${v.id} has no lineage` });
    if (v.kind === "derived" && !v.derivation)
      ctx.addIssue({ code: "custom", message: `derived value ${v.id} has no derivation` });
    if (v.kind === "source" && !v.source)
      ctx.addIssue({ code: "custom", message: `source value ${v.id} has no source reference` });
    if (v.kind !== "extracted" && !v.fmt)
      ctx.addIssue({ code: "custom", message: `${v.kind} value ${v.id} needs fmt` });
  });
export type Val = z.infer<typeof Val>;

export const ValRegistry = z.record(z.string(), Val).superRefine((reg, ctx) => {
  for (const [k, v] of Object.entries(reg)) {
    if (k !== v.id) ctx.addIssue({ code: "custom", message: `registry key ${k} != value id ${v.id}` });
  }
});
export type ValRegistry = z.infer<typeof ValRegistry>;

// ---------- manifest ----------

export const DataSource = z.object({
  id: z.string(),
  title: z.string(),
  publisher: z.string(),
  licence: z.string(),
  licence_url: z.string().url().optional(),
  attribution_html: z.string(),
  url: z.string().url(),
  retrieved_at: z.string(),
  redistributable: z.boolean(),
});
export type DataSource = z.infer<typeof DataSource>;

export const Artifact = z.object({
  path: z.string(),
  bytes: z.number().int().nonnegative(),
  sha256: z.string().length(64),
  count: z.number().int().nonnegative().optional(),
});

export const Manifest = z.object({
  schema_version: z.literal(SCHEMA_VERSION),
  build: z.object({
    id: z.string(),
    created_at: z.string(),
    pipeline_version: z.string(),
    page_images: z.boolean(),
    public_safe: z.boolean(),
    fixture: z.boolean(),
  }),
  bbox: z.tuple([z.number(), z.number(), z.number(), z.number()]),
  stats: ValRegistry, // m:compilation_collars, m:uranium_files, m:files_read, ...
  ref_points: z.array(z.object({ label: z.string(), lonlat: LonLat, shift_m: ValueId })),
  sources: z.array(DataSource),
  dictionaries: z.object({ companies: z.array(z.string()) }),
  artifacts: z.record(z.string(), Artifact),
});
export type Manifest = z.infer<typeof Manifest>;

// ---------- datum grid (raw arrays allowed: instrument data for the lens, not shown as numbers) ----------

export const DatumGrid = z
  .object({
    from: z.literal("EPSG:4267"),
    to: z.literal("EPSG:4269"),
    operation: z.string(),
    operation_code: z.string(),
    grid_file: z.string(),
    grid_sha256: z.string().length(64),
    proj_version: z.string(),
    pyproj_version: z.string(),
    accuracy_m: z.number(),
    lat0: z.number(),
    lon0: z.number(),
    dlat: z.number().positive(),
    dlon: z.number().positive(),
    nlat: z.number().int().positive(),
    nlon: z.number().int().positive(),
    order: z.string(),
    de_m: z.array(z.number()),
    dn_m: z.array(z.number()),
    stats: z.object({ min_m: z.number(), max_m: z.number(), mean_m: z.number() }),
    checks: z.array(
      z.object({
        label: z.string(),
        lon: z.number(),
        lat: z.number(),
        shift_m: z.number(),
        computed_m: z.number(),
        bearing_deg: z.number(),
      }),
    ),
    computed_at: z.string(),
  })
  .refine(
    (g) => g.de_m.length === g.nlat * g.nlon && g.dn_m.length === g.nlat * g.nlon,
    "grid size mismatch",
  );
export type DatumGrid = z.infer<typeof DatumGrid>;

// ---------- bulk layers (GeoJSON by URL; properties validated on a sample) ----------

export const CompilationProps = z.object({
  n: z.string(), // DRILLHOLE_NAME
  co: z.number().int().optional(), // index into manifest.dictionaries.companies
  y: z.number().int().optional(), // year drilled
  az: z.number().optional(),
  dip: z.number().optional(),
  len: z.number().optional(),
  u: z.union([z.literal(0), z.literal(1)]), // uranium tag
  rd: z.union([z.literal(0), z.literal(1)]), // belongs to one of the read files
  src: z.string().optional(), // SOURCE (assessment file numbers)
});
export const GeodsProps = z.object({
  n: z.string(),
  af: z.string().optional(), // assessment file number
  y: z.number().int().optional(),
  td: z.number().optional(),
  inc: z.number().optional(),
  az: z.number().optional(),
  dt: z.string().optional(), // UTM datum type as recorded
  rd: z.union([z.literal(0), z.literal(1)]),
});

export const YearHistogram = z.object({
  bins: z.array(z.object({ year: z.number().int(), cmp: ValueId, gds: ValueId })),
  undated_cmp: ValueId,
  undated_gds: ValueId,
  values: ValRegistry,
});
export type YearHistogram = z.infer<typeof YearHistogram>;

// ---------- reports ----------

export const Era = z.enum(["1970s", "1980s-1990s", "2000s+"]);

export const HoleStub = z.object({
  hole_id: z.string(),
  name: z.string(),
  status: z.enum(["pass", "flag", "miss"]),
  lonlat: LonLat.nullable(),
  position_source: z.enum([
    "extracted_transformed",
    "extracted_nad83",
    "provincial_geods",
    "provincial_compilation",
    "none",
  ]),
  datum_basis: z.enum(["printed", "none", "local_grid"]),
  geods_id: z.number().int().optional(),
  cmp_id: z.number().int().optional(),
});

export const ReportSummary = z.object({
  file_num: z.string(),
  company: z.string(),
  property: z.string().optional(),
  era: Era,
  year: ValueId,
  nts_sheets: z.array(z.string()),
  page_count: ValueId,
  scan_kind: z.enum(["scanned", "text", "mixed"]),
  file_sha256: z.string().length(64),
  source_url: z.string().url(),
  split: z.enum(["dev", "heldout"]),
  status_counts: z.object({ pass: ValueId, flag: ValueId, miss: ValueId }),
  footprint: z.object({ type: z.literal("Polygon"), coordinates: z.array(z.array(LonLat)) }),
  centroid: LonLat,
  holes: z.array(HoleStub),
});
export type ReportSummary = z.infer<typeof ReportSummary>;

export const ReportIndex = z.object({ reports: z.array(ReportSummary), values: ValRegistry });
export type ReportIndex = z.infer<typeof ReportIndex>;

export const Match = z.object({
  dataset: z.enum(["geods", "compilation"]),
  feature_id: z.number().int(),
  lonlat: LonLat,
  offset_m: ValueId,
  bearing_deg: ValueId,
  name_match: z.enum(["exact", "normalised", "fuzzy", "nearest"]),
  datum_shift_signature: z.boolean(),
  adjudication: z.enum(["none", "needed", "done"]),
});

export const Interval = z.object({
  id: z.string(),
  from: ValueId,
  to: ValueId,
  from_m: ValueId, // derived: ft_to_m or identity
  to_m: ValueId,
  code: ValueId.nullable(),
  description: ValueId.nullable(),
  table_id: z.string().nullable(),
  row: z.number().int().nullable(),
  status: z.enum(["pass", "flag", "miss"]),
  depth_unit_as_printed: z.string().nullable(),
});
export type Interval = z.infer<typeof Interval>;

export const AssayInterval = Interval.omit({ code: true, description: true }).extend({
  sample_id: ValueId.nullable(),
  grades: z.array(
    z.object({
      value: ValueId,
      analyte_as_printed: z.string().nullable(),
      species: z.enum(["U", "U3O8", "eU", "eU3O8", "other", "not_printed"]),
      basis: z.enum(["chemical", "probe_equivalent", "not_printed"]),
      method_as_printed: z.string().nullable(),
    }),
  ),
});
export type AssayInterval = z.infer<typeof AssayInterval>;

export const Transform = z.object({
  name: z.string(),
  code: z.string(),
  pipeline: z.string(),
  grid_file: z.string().nullable(),
  grid_sha256: z.string().nullable(),
  from: z.string(),
  to: z.string(),
  shift_m: ValueId,
  bearing_deg: ValueId,
  accuracy_m: z.number(),
});

export const Hole = z.object({
  hole_id: z.string(),
  name: ValueId,
  status: z.enum(["pass", "flag", "miss"]),
  collar: z.object({
    coord_kind: z.enum(["utm", "geographic", "local_grid", "not_printed"]),
    easting: ValueId.nullable(),
    northing: ValueId.nullable(),
    lat: ValueId.nullable(),
    lon: ValueId.nullable(),
    grid_x: ValueId.nullable(),
    grid_y: ValueId.nullable(),
    utm_zone: ValueId.nullable(),
    datum_printed: ValueId.nullable(), // null: the page states none
    elevation: ValueId.nullable(),
    azimuth: ValueId.nullable(),
    dip: ValueId.nullable(),
    total_depth: ValueId.nullable(),
  }),
  position: z
    .object({
      lonlat: LonLat,
      source: z.enum([
        "extracted_transformed",
        "extracted_nad83",
        "provincial_geods",
        "provincial_compilation",
      ]),
      lon: ValueId,
      lat: ValueId,
      transform: Transform.nullable(),
      misread_lonlat: LonLat.nullable(), // printed NAD27 numbers read as NAD83 (pipeline-computed)
      alt_lonlat: LonLat.nullable(), // no-datum case: the other candidate position
    })
    .nullable(),
  matches: z.array(Match),
  provincial_lith: z.array(Interval),
  lith: z.array(Interval),
  assays: z.array(AssayInterval),
});
export type Hole = z.infer<typeof Hole>;

export const TableRef = z.object({
  table_id: z.string(),
  page: z.number().int().min(1),
  bbox: BBox.nullable(),
  kind: z.enum(["collar", "assay", "lith", "probe"]),
  rows_stored: ValueId,
  rows_printed: ValueId.nullable(),
  continues_from: z.string().nullable(),
});

export const Report = z.object({
  summary: ReportSummary,
  values: ValRegistry,
  tables: z.array(TableRef),
  holes: z.array(Hole),
});
export type Report = z.infer<typeof Report>;

export const PagesIndex = z.object({
  file_num: z.string(),
  pages: z.array(
    z.object({
      page: z.number().int().min(1),
      width_px: z.number().int(),
      height_px: z.number().int(),
      kind: z.enum(["collar_table", "lith_log", "assay_table", "probe_log", "certificate", "other"]),
      image: z.string().nullable(), // null in public-safe builds
      thumb: z.string().nullable(),
      n_values: z.number().int().nonnegative(),
    }),
  ),
});
export type PagesIndex = z.infer<typeof PagesIndex>;

// ---------- eval ----------

const FieldScore = z.object({
  field: z.string(),
  n_gold: ValueId,
  tp: ValueId,
  fn: ValueId,
  fp: ValueId,
  recall: ValueId,
  precision: ValueId,
  miss_value_ids: z.array(z.string()),
});

const SplitScore = z.object({
  fields: z.array(FieldScore),
  traps: z.array(z.object({ trap: z.string(), n_gold: ValueId, recovered: ValueId, recall: ValueId })),
  class_a: z.object({ n: ValueId, misses: ValueId, rate: ValueId, upper95: ValueId, method: z.string() }),
  coord: z.object({
    n: ValueId,
    median_m: ValueId,
    p90_m: ValueId,
    max_m: ValueId,
    datum_stated_share: ValueId,
    per_hole: z.array(z.object({ file_num: z.string(), hole_id: z.string(), offset_m: ValueId })),
  }),
  evidence_validity: z.object({ n: ValueId, share: ValueId }),
  unbacked_numbers: ValueId,
});

export const Scorecard = z.object({
  run_id: z.string(),
  config_id: z.string(),
  created_at: z.string(),
  label_provenance: z.literal("model-assisted labels"),
  labeller: z.string(),
  values: ValRegistry,
  gold: z.object({
    files: ValueId,
    tables: ValueId,
    intervals: ValueId,
    field_values: ValueId,
    dev_files: ValueId,
    heldout_files: ValueId,
    blind_tables: ValueId,
    prefill_edit: z.object({
      n_prefilled: ValueId,
      n_changed: ValueId,
      n_rows_added: ValueId,
      n_rows_deleted: ValueId,
    }),
    anchoring_gap: ValueId.nullable(),
    agreement: z.array(z.object({ metric: z.string(), value: ValueId, n: ValueId })),
  }),
  splits: z.object({ dev: SplitScore.nullable(), heldout: SplitScore.nullable() }),
  heldout_run_count: z.number().int().nonnegative(),
});
export type Scorecard = z.infer<typeof Scorecard>;

export const FailureCard = z.object({
  id: z.string(),
  real: z.literal(true),
  failure_class: z.string(),
  file_num: z.string(),
  page: z.number().int().min(1),
  table_id: z.string(),
  values: ValRegistry,
  rows_printed: ValueId,
  rows_stored: ValueId,
  missing_rows: z.array(z.object({ bbox: BBox, quote: z.string().nullable() })),
  why_uncaught: z.string(),
  consequence: z.string(),
  fix: z.string(),
  before: z.object({ recall: ValueId, upper95: ValueId, n: ValueId, config_id: z.string() }),
  after: z.object({ recall: ValueId, upper95: ValueId, n: ValueId, config_id: z.string() }),
  still_unknown: z.string(),
});
export type FailureCard = z.infer<typeof FailureCard>;

// ---------- run summary (statistics before any gold labels exist) ----------

const StatRef = ValueId;

export const RunSummary = z.object({
  schema_version: z.literal("1.0.0"),
  generated_at: z.string(),
  pipeline_version: z.string(),
  /** null until a gold set has been labelled and `lr score` has written a scorecard. */
  gold: z.null(),
  caveats: z.array(z.string()),
  run: z.object({
    models: z.array(z.string()),
    run_ids: z.array(z.string()),
    calls: StatRef,
    pages_read: StatRef,
    cost_usd: StatRef,
    tokens_in: StatRef,
    tokens_out: StatRef,
    minutes: StatRef,
  }),
  coverage: z.object({ files_read: StatRef, pages_read: StatRef, tables: StatRef, holes: StatRef }),
  reading: z.object({
    printed_values: StatRef,
    located: StatRef,
    located_share: StatRef,
    empty_cells: StatRef,
    empty_cells_boxed: StatRef,
    illegible: StatRef,
    digit_exact: StatRef,
    digit_confusable: StatRef,
    digit_mismatch: StatRef,
  }),
  placement: z.object({ from_page: StatRef, from_provincial: StatRef, not_placed: StatRef }),
  crosscheck: z.object({
    matches: StatRef,
    independent: StatRef,
    median_offset_m: StatRef,
    max_offset_m: StatRef,
    datum_shift_signatures: StatRef,
  }),
  checks: z.array(
    z.object({
      id: z.string(),
      title: z.string(),
      severity: z.enum(["info", "warn", "error"]),
      flags: StatRef,
      class_a: StatRef,
      examples: z.array(
        z.object({
          value_id: z.string(),
          file_num: z.string(),
          page: z.number().int().nullable(),
          message: z.string(),
        }),
      ),
    }),
  ),
  per_file: z.array(
    z.object({ file_num: z.string(), values: StatRef, located: StatRef, holes: StatRef, tables: StatRef }),
  ),
  values: ValRegistry,
  build_id: z.string(),
});
export type RunSummary = z.infer<typeof RunSummary>;

// ---------- prospect: what the data can support, before anything is scored

/**
 * The readiness scorecard. It is deliberately not a score of ground: it reports how much of the basin each
 * feature actually covers, what each source is and under which licence, and what does not exist at all.
 */
export const ProspectFeature = z.object({
  feature_key: z.string(),
  title: z.string(),
  bears_on: z.string(),
  is_effort: z.boolean(), // an exploration-effort feature: the null model's world, not geology
  unit: z.string().nullable(),
  thin: z.boolean(), // coverage at or below the threshold: cannot carry a basin-wide model on its own
  coverage: StatRef,
  covered_cells: StatRef,
  median_obs: StatRef.nullable(),
  median_value: StatRef.nullable(),
  notes: z.string(),
  sources: z.array(z.string()),
});
export type ProspectFeature = z.infer<typeof ProspectFeature>;

export const ProspectSource = z.object({
  key: z.string(),
  title: z.string(),
  role: z.enum(["feature", "label", "context"]),
  bears_on: z.string(),
  tier: z.enum(["native", "read", "derived"]),
  access: z.enum(["arcgis_rest", "stac", "file", "internal"]),
  url: z.string(),
  licence: z.string(),
  licence_url: z.string(),
  redistributable: z.boolean(),
  verified: z.boolean(),
  verified_at: z.string(),
  record_count: z.number().int().nullable(),
  notes: z.string(),
  caveats: z.array(z.string()),
});
export type ProspectSource = z.infer<typeof ProspectSource>;

export const ProspectGap = z.object({
  key: z.string(),
  title: z.string(),
  status: z.enum(["not_addressable", "not_published", "not_public", "unverified", "published_not_pulled"]),
  why_it_matters: z.string(),
  evidence: z.string(),
  workaround: z.string(),
});
export type ProspectGap = z.infer<typeof ProspectGap>;

export const MetricRow = z.object({
  model: z.enum(["criteria", "learned", "effort"]),
  fold: z.enum(["none", "random", "spatial", "camp"]),
  metric: z.enum(["pr_auc", "roc_auc", "capture_top10", "base_rate"]),
  value_id: StatRef,
});
export type MetricRow = z.infer<typeof MetricRow>;

/** One metric of one configuration of the Phase 0 re-test (`lr prospect headline`), with its tracker run. */
export const HeadlineRow = z.object({
  config: z.string(),
  feature_set: z.enum(["learned", "effort"]),
  positives: z.enum(["all", "deposits"]),
  matched: z.boolean(),
  thinned: z.boolean(),
  fold: z.enum(["random", "spatial", "camp"]),
  metric: z.string(),
  run_id: z.string(),
  value_id: StatRef,
  /** The two bounds of the bootstrap interval, as values of their own. */
  ci: z.tuple([StatRef, StatRef]).optional(),
});
export type HeadlineRow = z.infer<typeof HeadlineRow>;

/** One metric of one arm of the model search (`lr prospect modelsearch`); `run_id` is that arm's MLflow run. */
export const SearchRow = z.object({
  arm: z.string(),
  name: z.string(),
  feature_set: z.string(),
  fold: z.string(),
  positives: z.string(),
  metric: z.string(),
  run_id: z.string(),
  value_id: StatRef,
  ci: z.tuple([StatRef, StatRef]).optional(),
});
export type SearchRow = z.infer<typeof SearchRow>;

/** Where one later discovery ranked under one model frozen at a cutoff, as a share of basin area. */
export const HindcastRow = z.object({
  cutoff: z.number().int(),
  model: z.enum(["criteria", "learned", "effort"]),
  discovery: z.string(),
  title: z.string(),
  year: z.number().int().nullable(),
  confidence: z.string().nullable(),
  cells: z.array(z.string()),
  run_id: z.string(),
  value_id: StatRef,
});
export type HindcastRow = z.infer<typeof HindcastRow>;

const RunNaming = {
  run_id: z.string(),
  store_sha256: z.string().nullable().optional(),
  snapshot: z.string().nullable().optional(),
};

export const HeadlineBlock = z.object({
  ...RunNaming,
  mlflow_run_id: z.string().nullable().optional(),
  cells: StatRef.nullable(),
  verdict: z.string().nullable(),
  rows: z.array(HeadlineRow),
  minetrace: z.array(
    z.object({ feature_set: z.string(), metric: z.string(), run_id: z.string(), value_id: StatRef }),
  ),
});
export type HeadlineBlock = z.infer<typeof HeadlineBlock>;

export const SearchBlock = z.object({
  ...RunNaming,
  quick: z.boolean(),
  cells: StatRef.nullable(),
  rows: z.array(SearchRow),
  decision: z
    .object({
      model: z.string().nullable(),
      stage: z.string().nullable(),
      served: z.boolean(),
      reason: z.string(),
      run_id: z.string().nullable(),
      version: z.number().int().nullable(),
      /** The model card from the tracker's row for the registered run. */
      card: z
        .object({
          name: z.string(),
          feature_set: z.string(),
          fold: z.string(),
          positives: z.string(),
          matched: z.boolean(),
          thinned: z.boolean(),
          features: z.array(z.string()),
          n_pos: StatRef.nullable(),
          scored: StatRef.nullable(),
        })
        .nullable()
        .optional(),
    })
    .nullable(),
});
export type SearchBlock = z.infer<typeof SearchBlock>;

export const HindcastBlock = z.object({
  ...RunNaming,
  rows: z.array(HindcastRow),
  summary: z.array(z.object({ model: z.string(), key: z.string(), run_id: z.string(), value_id: StatRef })),
});
export type HindcastBlock = z.infer<typeof HindcastBlock>;

/** The metrics a benchmark row prints; a metric the table could not compute (NaN) is simply absent. */
export const BenchMetric = z.enum([
  "f1",
  "precision",
  "recall",
  "pr_auc",
  "roc_auc",
  "pr_auc_all",
  "roc_auc_all",
  "ece",
  "abstain_rate",
]);
export type BenchMetric = z.infer<typeof BenchMetric>;

/** What only an arm can report: the gate's refusals, the probes, and what a cell cost. */
export const BenchExtra = z.enum([
  "gate_rejection_rate",
  "probe_abstain_rate",
  "cost_usd_per_cell",
  "latency_s_per_cell",
]);
export type BenchExtra = z.infer<typeof BenchExtra>;

/**
 * The staged loop's per-stage columns (PRD §8.5), per chain: what only a staged arm reports. The node gate's
 * refusals over executor attempts; the share of chains a verifier round validated and the share the verifier
 * refused at least once; rounds over the chains that validated; nodes re-executed on the verifier's feedback;
 * and how often the verifier's own label and the weighted-sum decider agreed with the final verdict. A
 * single-call arm has no stages and carries none.
 */
export const BenchStage = z.enum([
  "n_chains",
  "gate_rejection_rate",
  "valid_rate",
  "verifier_catch_rate",
  "rounds_to_valid_mean",
  "reexecuted_mean",
  "verifier_agreement_rate",
  "decider_agreement_rate",
]);
export type BenchStage = z.infer<typeof BenchStage>;

/**
 * One row of the analyst benchmark table (`lr bench table`): one arm or one baseline scored on the same open
 * cells of the frozen benchmark. Every number is a value id under `c:bench:<version>:<row>:<metric>`; an
 * interval's bounds are `.lo` and `.hi` of the metric's id, and a stage column sits under `:stage:<key>`. A
 * baseline is arithmetic on the fitted scores, so it names no run.
 */
export const BenchRow = z.object({
  name: z.string(),
  kind: z.enum(["arm", "baseline"]),
  model: z.string().nullable(),
  /** The reasoning effort the arm ran at (low, medium, high); null for a baseline. */
  effort: z.string().nullable().optional(),
  n: StatRef,
  n_pos: StatRef.nullable(),
  n_neg: StatRef.nullable(),
  run_id: z.string().nullable(),
  mlflow_run_id: z.string().nullable(),
  note: z.string().optional(),
  metrics: z.partialRecord(BenchMetric, StatRef),
  ci: z.partialRecord(BenchMetric, z.tuple([StatRef, StatRef])).optional(),
  extra: z.partialRecord(BenchExtra, StatRef).optional(),
  /** Present only on a staged arm; a stage the run never had (no verifier, no rounds) is simply absent. */
  stages: z.partialRecord(BenchStage, StatRef).optional(),
  strata: z
    .partialRecord(
      z.enum(["deposit", "occurrence", "negative"]),
      z.object({ n: StatRef.optional(), accuracy: StatRef.optional(), abstain_rate: StatRef.optional() }),
    )
    .optional(),
});
export type BenchRow = z.infer<typeof BenchRow>;

/** The analyst benchmark table for the highest benchmark version that has one; earlier versions are named. */
export const BenchBlock = z.object({
  version: z.string(),
  manifest_sha256: z.string().nullable(),
  computed_at: z.string().nullable(),
  rows: z.array(BenchRow),
  versions: z.array(z.string()),
});
export type BenchBlock = z.infer<typeof BenchBlock>;

/** One column of the five-column readiness gate: ok or not, and the gate's own note saying why. */
export const GateCell = z.object({ ok: z.boolean(), note: z.string() });

/** The data readiness gate (`lr prospect gate`): one row per dataset the focused tasks depend on. */
export const ReadinessGate = z.object({
  generated_at: z.string(),
  store_sha256: z.string().nullable(),
  snapshot: z.string().nullable(),
  green: z.boolean(),
  rows: z.array(
    z.object({
      dataset: z.string(),
      title: z.string(),
      kind: z.enum(["layer", "scene", "label", "file"]),
      present: GateCell,
      licensed: GateCell,
      covers: GateCell,
      servable: GateCell,
      versioned: GateCell,
    }),
  ),
  failures: z.array(z.string()),
});
export type ReadinessGate = z.infer<typeof ReadinessGate>;

export const Readiness = z.object({
  schema_version: z.literal(SCHEMA_VERSION),
  generated_at: z.string(),
  build_id: z.string(),
  grid: z.object({
    grid_id: z.string(),
    epsg: z.number().int(),
    cells: StatRef,
    cell_m: StatRef,
    in_basin: StatRef,
    area_km2: StatRef,
    buffer_km: StatRef,
  }),
  totals: z.object({
    sources: StatRef,
    verified_features: StatRef,
    redistributable: StatRef,
    gaps: StatRef,
    geo_features: StatRef,
    effort_features: StatRef,
  }),
  thin_coverage_threshold: z.number(),
  metrics: z.object({ run_id: z.string().nullable(), rows: z.array(MetricRow) }).optional(),
  /** The five-column readiness gate, as last run; absent until `lr prospect gate` has run. */
  readiness_gate: ReadinessGate.optional(),
  /** Phase 0: the effort-against-geology re-test with corrections, intervals and the MineTRACE protocol. */
  headline: HeadlineBlock.optional(),
  /** Phase 3: every arm of the model search, each naming its tracker run, and the served-model decision. */
  search: SearchBlock.optional(),
  /** Phase 3: the dated hindcast, later discoveries ranked by models frozen at a cutoff. */
  hindcast: HindcastBlock.optional(),
  /** The analyst benchmark: every arm and baseline on the frozen benchmark's open cells (`lr bench table`). */
  bench: BenchBlock.optional(),
  /** What the fabrication gate scored on the adversarial suite: `lr prospect gate-eval`. */
  gate: z
    .object({
      run_id: z.string(),
      cells: z.array(z.string()),
      cases: StatRef,
      put: StatRef,
      caught: StatRef,
      missed: StatRef,
      caught_rate: StatRef,
      honest: StatRef,
      wrongly_rejected: StatRef,
      by_kind: z.record(z.string(), z.object({ caught: StatRef, missed: StatRef })),
      escaped: z.array(z.object({ kind: z.string(), text: z.string(), note: z.string() })),
    })
    .optional(),
  caveats: z.array(z.string()),
  features: z.array(ProspectFeature),
  sources: z.array(ProspectSource),
  gaps: z.array(ProspectGap),
  values: ValRegistry,
});
export type Readiness = z.infer<typeof Readiness>;

// ---------- prospect scores: what the three models say, and what they are worth

// ---------- the analyst chain: what the staged loop produced over a cell (PRD §8.4 Stage 6, §9.4)

/** The three-level verdict every decider speaks. The panel renders each level as words, never as a colour. */
export const ChainVerdict = z.enum(["evidence_against", "insufficient", "supports_closer_look"]);
export type ChainVerdict = z.infer<typeof ChainVerdict>;

/** A node's reading of its criterion: `unknown` means never measured here, which is not `not_met`. */
export const ChainNodeStatus = z.enum(["met", "not_met", "unknown"]);
export type ChainNodeStatus = z.infer<typeof ChainNodeStatus>;

/**
 * One node as the chain stands: the executor's last attempt at it. Its numbers live in `value_ids`; `strength`
 * is served as the store holds it and printed through the stat the service registers for it, as a score is.
 */
export const ChainNode = z.object({
  node_id: z.string(),
  segment_id: z.string(),
  kind: z.enum(["criterion", "crosscheck", "retrieval"]),
  criterion: z.string().nullable(),
  status: ChainNodeStatus,
  strength: z.number().int().min(0).max(5),
  value_ids: z.array(z.string()),
  /** those of the cited ids that are expert-tier, labelled as such in every node that cites them (B19) */
  expert_ids: z.array(z.string()),
  depends_on: z.array(z.string()),
  text: z.string(),
  published: z.boolean(),
  problems: z.array(z.string()),
  round: z.number().int(),
  attempt: z.number().int(),
});
export type ChainNode = z.infer<typeof ChainNode>;

/** One chain-verify round. The candidate label is the verifier's own answer, recorded and never acted on. */
export const ChainRound = z.object({
  round: z.number().int(),
  valid: z.boolean(),
  faulty: z.array(z.object({ node_id: z.string(), reason: z.string() })),
  feedback: z.string().nullable(),
  candidate_label: ChainVerdict.nullable(),
  candidate_probability: z.number().nullable(),
  rationale: z.string().nullable(),
});
export type ChainRound = z.infer<typeof ChainRound>;

/** The adjudicator's answer, gated like a memo: withheld claims are kept as a record, not offered as argument. */
export const ChainDecision = z.object({
  verdict: ChainVerdict,
  probability: z.number().nullable(),
  claims: z.array(z.object({ text: z.string(), value_ids: z.array(z.string()) })),
  unknown_criteria: z.array(z.string()),
  absent_criteria: z.array(z.string()),
  next_observation: z.string().nullable(),
  rationale: z.string().nullable(),
  published: z.boolean(),
  problems: z.array(z.string()),
});
export type ChainDecision = z.infer<typeof ChainDecision>;

/**
 * One analyst chain over a cell. Both deciders are carried: the weighted sum with the weights it used, and
 * the adjudicator's decision; `majority_label` is the vote over rounds when K was exhausted, and a chain that
 * never validated publishes `insufficient` with `abstained_reason` set.
 */
export const AnalystChain = z.object({
  chain_id: z.string(),
  run_id: z.string(),
  arm: z.string(),
  purpose: z.enum(["dashboard", "scored", "benchmark"]),
  planner: z.enum(["template", "model"]),
  rounds: z.number().int(),
  valid: z.boolean(),
  final_verdict: ChainVerdict,
  final_probability: z.number().nullable(),
  weighted_score: z.number().nullable(),
  weights_version: z.string().nullable(),
  verifier_label: ChainVerdict.nullable(),
  majority_label: ChainVerdict.nullable(),
  abstained_reason: z.string().nullable(),
  published: z.boolean(),
  created_at: z.string(),
  cost_usd: z.number().nullable(),
  nodes: z.array(ChainNode),
  verdicts: z.array(ChainRound),
  decision: ChainDecision.nullable(),
});
export type AnalystChain = z.infer<typeof AnalystChain>;

/** The evidence record for one cell, served by the local agent service (never bundled into the static build). */
export const CellEvidence = z.object({
  cell_id: z.string(),
  lon: z.number().nullable(),
  lat: z.number().nullable(),
  in_basin: z.boolean().nullable(),
  parts: z.record(
    z.string(),
    z.object({
      tool: z.string(),
      args: z.record(z.string(), z.unknown()),
      note: z.string(),
      rows: z.array(z.record(z.string(), z.unknown())),
      values: ValRegistry,
    }),
  ),
  values: ValRegistry,
  memos: z.array(
    z.object({
      memo_id: z.string(),
      role: z.enum(["proponent", "skeptic", "adjudicator"]),
      verdict: z.string().nullable(),
      published: z.boolean(),
      created_at: z.string(),
      claims: z.array(z.object({ claim_no: z.number(), text: z.string(), value_ids: z.array(z.string()) })),
    }),
  ),
  /** The cell's analyst chains, newest first; a record from before the chains existed simply has none. */
  chains: z.array(AnalystChain).default([]),
});
export type CellEvidence = z.infer<typeof CellEvidence>;

/** One answer from the conversational agent. `text` is null when the answer failed the evidence check. */
export const ChatTurn = z.object({
  question: z.string(),
  text: z.string().nullable(),
  claims: z.array(z.object({ text: z.string(), value_ids: z.array(z.string()) })).default([]),
  caveats: z.array(z.string()).default([]),
  cannot_answer: z.boolean().default(false),
  published: z.boolean(),
  problems: z.array(z.string()).default([]),
  tools_used: z.array(z.string()).default([]),
  cost_usd: z.number().optional(),
});
export type ChatTurn = z.infer<typeof ChatTurn>;

/**
 * A conversation that actually happened, recorded so the walkthrough can show one without waiting on a model
 * or on a server being up. The turns are the gate's own record: a withheld one is kept, with its objection.
 */
export const RecordedChat = z.object({
  cell_id: z.string(),
  lon: z.number().nullable(),
  lat: z.number().nullable(),
  recorded_at: z.string(),
  model: z.string(),
  turns: z.array(ChatTurn),
  values: ValRegistry,
});
export type RecordedChat = z.infer<typeof RecordedChat>;

export const ChatResponse = z.object({
  conversation_id: z.string(),
  cell_id: z.string(),
  turn: ChatTurn,
  values: ValRegistry,
  cost_usd: z.number().optional(),
});
export type ChatResponse = z.infer<typeof ChatResponse>;

/** A conversation the service persisted, turn by turn: the benchmark's raw material, and resumable. */
export const ConversationSummary = z.object({
  conversation_id: z.string(),
  model: z.string(),
  backend: z.string(),
  created_at: z.string(),
  turns: z.number().int(),
  cost_usd: z.number(),
});
export type ConversationSummary = z.infer<typeof ConversationSummary>;
export const CellConversations = z.object({
  cell_id: z.string(),
  conversations: z.array(ConversationSummary),
});
export type CellConversations = z.infer<typeof CellConversations>;
export const StoredTurn = z.object({
  step: z.number().int(),
  question: z.string(),
  text: z.string().nullable(),
  published: z.boolean(),
  problems: z.array(z.string()),
  claims: z.array(z.object({ text: z.string(), value_ids: z.array(z.string()) })),
  tool_calls: z.array(z.object({ tool: z.string() }).passthrough()),
  values: ValRegistry,
  cost_usd: z.number().nullable(),
  duration_s: z.number().nullable(),
  created_at: z.string(),
});
export type StoredTurn = z.infer<typeof StoredTurn>;
export const ConversationRecord = z.object({
  conversation_id: z.string(),
  cell_id: z.string(),
  model: z.string(),
  backend: z.string(),
  created_at: z.string(),
  turns: z.array(StoredTurn),
});
export type ConversationRecord = z.infer<typeof ConversationRecord>;
export const Candidate = z.object({
  cell_id: z.string(),
  score: z.number(),
  known_share: z.number().nullable(),
  lon: z.number(),
  lat: z.number(),
  in_basin: z.boolean(),
  label_tier: z.string().nullable(),
  label_name: z.string().nullable(),
  km_to_label: z.number().nullable(),
});
export type Candidate = z.infer<typeof Candidate>;

// ---------------------------------------------------------------- the interface agent (PRD §8.3, Phase 4c)

/**
 * The kinds the intent router picks from. Each has a plan of tool calls written in Python: the model decides
 * what is being asked, never what the answer is. `other` is the plain tool loop, capped at a few calls.
 */
export const RouteKind = z.enum([
  "lookup",
  "compare",
  "explain_score",
  "what_is_unknown",
  "what_would_change",
  "record_insight",
  "run_analyst",
  "other",
]);
export type RouteKind = z.infer<typeof RouteKind>;

/** The route a turn took: the kind, what it named, whether it was out of scope, and the plan that followed. */
export const ChatRoute = z.object({
  kind: RouteKind,
  topic: z.string().nullable().default(null),
  cell_ids: z.array(z.string()).default([]),
  entities: z.array(z.string()).default([]),
  out_of_scope: z.boolean().default(false),
  detail: z.string().default(""),
  reason: z.string().default(""),
  /** set when the router's reply could not be read and the turn fell back to the loop */
  fallback: z.string().nullable().default(null),
  meaning: z.string().default(""),
  plan: z.array(z.object({ tool: z.string(), args: z.record(z.string(), z.unknown()) })).default([]),
});
export type ChatRoute = z.infer<typeof ChatRoute>;

/** Why the agent declined: the four reasons the abstain tool takes (PRD §E.3), never a free-text shrug. */
export const AbstainReason = z.enum(["not_measured", "outside_grid", "no_value", "out_of_scope"]);
export type AbstainReason = z.infer<typeof AbstainReason>;

/** A refusal, recorded under its own id so it can be counted; `detail` is the model's words, shown only if gated. */
export const ChatAbstention = z.object({
  abstain_id: z.string(),
  reason: AbstainReason,
  detail: z.string().default(""),
  said: z.string().default(""),
  recorded_at: z.string().optional(),
});
export type ChatAbstention = z.infer<typeof ChatAbstention>;

/** A geologist's statement written to the expert tier; the numbers in it now carry expert-tier value ids (B19). */
export const ChatInsight = z.object({
  expert_id: z.string(),
  author: z.string(),
  text: z.string(),
  value_ids: z.array(z.string()).default([]),
  recorded_at: z.string().nullable().optional(),
});
export type ChatInsight = z.infer<typeof ChatInsight>;

/**
 * The session assessment: a finished analyst job beside the cell's stored chain without the insight, node by
 * node and verdict by verdict. Words, not numbers: the counts are of the rows in this very object.
 */
export const ChainDiff = z.object({
  chain_id: z.string().nullable().optional(),
  baseline_chain_id: z.string().nullable().optional(),
  verdict: z.object({
    before: z.string().nullable(),
    after: z.string().nullable(),
    changed: z.boolean(),
  }),
  nodes: z.array(
    z.object({
      node_id: z.string(),
      criterion: z.string().nullable().optional(),
      kind: z.string().nullable().optional(),
      before: z.string().nullable(),
      after: z.string().nullable(),
      expert_ids: z.array(z.string()).default([]),
      changed: z.boolean(),
    }),
  ),
  n_changed: z.number().int(),
  n_leaning_on_expert: z.number().int().optional(),
  expert_ids: z.array(z.string()).default([]),
  note: z.string().default(""),
});
export type ChainDiff = z.infer<typeof ChainDiff>;

/** An analyst job the conversation submitted: the handover, and once it is done the verdict and the diff. */
export const ChatJob = z
  .object({
    job_id: z.string(),
    cell_id: z.string(),
    reason: z.string().default(""),
    expert_ids: z.array(z.string()).default([]),
    score_ids: z.array(z.string()).default([]),
    budget_usd: z.number().optional(),
    requested_by: z.string().optional(),
    submitted_at: z.string().optional(),
    status: z.string().default("submitted"),
    verdict: z.string().nullable().optional(),
    result: z.record(z.string(), z.unknown()).nullable().optional(),
    assessment: ChainDiff.nullable().optional(),
    assessment_error: z.string().optional(),
    error: z.string().optional(),
  })
  .passthrough();
export type ChatJob = z.infer<typeof ChatJob>;

/** A chat turn as the interface agent returns it: the old shape plus the route and the actions it took. */
export const InterfaceTurn = ChatTurn.extend({
  route: ChatRoute.nullable().optional(),
  abstention: ChatAbstention.nullable().optional(),
  insight: ChatInsight.nullable().optional(),
  job: ChatJob.nullable().optional(),
  jobs_done: z.array(ChatJob).default([]),
  expert_ids: z.array(z.string()).default([]),
  retried: z.boolean().default(false),
  model: z.string().optional(),
});
export type InterfaceTurn = z.infer<typeof InterfaceTurn>;
export const InterfaceChatResponse = ChatResponse.extend({ turn: InterfaceTurn });
export type InterfaceChatResponse = z.infer<typeof InterfaceChatResponse>;

// ---------- background jobs (PRD §A.2): the service's `agent.job` rows, polled while one runs ----------

export const JobStatus = z.enum(["queued", "running", "done", "failed", "cancelled"]);
export type JobStatus = z.infer<typeof JobStatus>;
/** One event on a job's row: the stages as they happen (`stage:plan`, `stage:verify`), a run claimed, a log line. */
export const JobEvent = z.object({ at: z.string(), event: z.string() }).passthrough();
export type JobEvent = z.infer<typeof JobEvent>;
/** What an analyst job produced: the chain the evidence panel now lists, its verdict and its cost. Other
 *  kinds will shape their own results; the strip prints only what it knows. */
export const AnalystJobResult = z
  .object({
    chain_id: z.string(),
    verdict: z.string().nullable().optional(),
    published: z.boolean().optional(),
    cost_usd: z.number().optional(),
    run_id: z.string().optional(),
  })
  .passthrough();
export const Job = z.object({
  job_id: z.string(),
  kind: z.string(),
  cell_id: z.string().nullable(),
  status: JobStatus,
  requested_by: z.string(),
  args: z.record(z.string(), z.unknown()),
  created_at: z.string(),
  started_at: z.string().nullable(),
  finished_at: z.string().nullable(),
  progress: z.array(JobEvent),
  result: z.record(z.string(), z.unknown()).nullable(),
  error: z.string().nullable(),
  run_id: z.string().nullable(),
});
export type Job = z.infer<typeof Job>;
export const CellJobs = z.object({ cell_id: z.string(), jobs: z.array(Job) });
export type CellJobs = z.infer<typeof CellJobs>;
/** Who the key resolves to, from `/api/whoami`: a label that is never the key, and the roles it holds. */
export const Whoami = z.object({ name: z.string(), scopes: z.array(z.string()), roles: z.array(z.string()) });
export type Whoami = z.infer<typeof Whoami>;

// ---------- the extractor's review queue (PRD §8.2 stage 4): what the second reader disagreed on, served by /api/review

/** One reader's view of a value, as the comparer recorded it; `bbox` is the value's own located box, `row_bbox` its row's band. */
export const ReviewReading = z
  .object({
    field: z.string(),
    scope: z.string(),
    as_printed: z.string(),
    unit_as_printed: z.string().nullable().optional(),
    analyte: z.string().nullable().optional(),
    quote: z.string().nullable().optional(),
    bbox: BBox.nullable().optional(),
    row_bbox: BBox.nullable().optional(),
    row_index: z.number().int().nullable().optional(),
    value_id: z.string().nullable().optional(),
    model: z.string().nullable().optional(),
  })
  .passthrough();
export type ReviewReading = z.infer<typeof ReviewReading>;

export const ReviewStatus = z.enum(["open", "accepted_a", "accepted_b", "rejected", "edited"]);
export type ReviewStatus = z.infer<typeof ReviewStatus>;

/** A queue item: a disagreement, or a value only one reader found. A resolution never rewrites the two readings. */
export const ReviewItem = z.object({
  queue_id: z.string(),
  file_num: z.string(),
  page: z.number().int().min(1),
  page_id: z.string().nullable().optional(),
  field: z.string(),
  field_type: z.string().nullable().optional(),
  value_id: z.string().nullable().optional(),
  reading_a: ReviewReading.nullable(),
  reading_b: ReviewReading.nullable(),
  reason: z.enum(["disagreed", "only_a", "only_b"]),
  status: ReviewStatus,
  run_id: z.string(),
  model_a: z.string().nullable().optional(),
  model_b: z.string().nullable().optional(),
  created_at: z.string(),
  resolved_by: z.string().nullable().optional(),
  resolved_at: z.string().nullable().optional(),
  resolution: z.record(z.string(), z.unknown()).nullable().optional(),
});
export type ReviewItem = z.infer<typeof ReviewItem>;

export const ReviewQueuePage = z.object({
  items: z.array(ReviewItem),
  total: z.number().int(),
  limit: z.number().int(),
  offset: z.number().int(),
  status: z.string(),
  file_num: z.string().nullable().optional(),
  counts: z.record(z.string(), z.number().int()),
});
export type ReviewQueuePage = z.infer<typeof ReviewQueuePage>;
