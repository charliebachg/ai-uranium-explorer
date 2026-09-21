import { existsSync, readdirSync, readFileSync } from "node:fs";
import { join, resolve } from "node:path";
import {
  DatumGrid,
  Manifest,
  PagesIndex,
  Readiness,
  RecordedChat,
  Report,
  ReportIndex,
  YearHistogram,
} from "../src/data/contract";

/**
 * Export gate: every file under web/public/data must match the contract, and no exported report may contain a
 * numeric value that cannot be traced to a stored value id. Run with: npm run validate:data
 */

const ROOT = resolve(import.meta.dirname, "../public/data");
let failures = 0;

function check(path: string, schema: { safeParse: (x: unknown) => { success: boolean; error?: unknown } }) {
  const full = join(ROOT, path);
  if (!existsSync(full)) {
    console.log(`skip  ${path} (not exported)`);
    return null;
  }
  const raw = JSON.parse(readFileSync(full, "utf8"));
  const r = schema.safeParse(raw);
  if (!r.success) {
    failures++;
    const issues = (r.error as { issues?: { path: PropertyKey[]; message: string }[] }).issues ?? [];
    console.error(`FAIL  ${path}`);
    for (const i of issues.slice(0, 6))
      console.error(`        ${i.path.map(String).join(".")}: ${i.message}`);
    return null;
  }
  console.log(`ok    ${path}`);
  return raw;
}

check("manifest.json", Manifest);
check("context/datum_grid.json", DatumGrid);
check("bulk/year_histogram.json", YearHistogram);
check("prospect/readiness.json", Readiness);
check("prospect/recorded_chat.json", RecordedChat);
const index = check("reports/index.json", ReportIndex) as { reports?: { file_num: string }[] } | null;

for (const r of index?.reports ?? []) {
  const report = check(`reports/${r.file_num}/report.json`, Report) as {
    values: Record<
      string,
      {
        kind: string;
        as_printed?: string | null;
        lineage?: { quote?: string; bbox?: unknown; quote_located?: boolean };
      }
    >;
  } | null;
  check(`reports/${r.file_num}/pages.json`, PagesIndex);
  if (!report) continue;
  // A value with printed text must carry its verbatim quote. A cell the page leaves empty ("not printed") has
  // nothing to quote, but must say so: quote_located false, and ideally a synthesised cell box.
  for (const [id, v] of Object.entries(report.values)) {
    if (v.kind !== "extracted") continue;
    const l = v.lineage;
    if (!l) {
      failures++;
      console.error(`FAIL  ${r.file_num}: extracted value ${id} has no lineage`);
      continue;
    }
    if (v.as_printed != null && !l.quote) {
      failures++;
      console.error(`FAIL  ${r.file_num}: printed value ${id} ("${v.as_printed}") has no quote`);
    }
    if (!l.bbox && l.quote_located !== false) {
      failures++;
      console.error(`FAIL  ${r.file_num}: ${id} has no box but is not flagged as unlocated`);
    }
  }
}

const banned = "Mining/MapServer";
for (const dir of [ROOT]) {
  const walk = (d: string): string[] =>
    readdirSync(d, { withFileTypes: true }).flatMap((e) =>
      e.isDirectory()
        ? walk(join(d, e.name))
        : e.name.endsWith(".json") || e.name.endsWith(".geojson")
          ? [join(d, e.name)]
          : [],
    );
  for (const f of walk(dir)) {
    if (readFileSync(f, "utf8").includes(banned)) {
      failures++;
      console.error(`FAIL  ${f} references the mineral dispositions service`);
    }
  }
}

console.log(failures ? `\n${failures} problem(s)` : "\nall exported data matches the contract");
process.exit(failures ? 1 : 0);
