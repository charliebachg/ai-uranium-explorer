"""`lr` command line. Stages are added here as they are built."""

from __future__ import annotations

import json
import time

import typer

from . import crs
from .doctor import run_checks
from .paths import PATHS

app = typer.Typer(no_args_is_help=True, add_completion=False, help="Legacy Reader pipeline")
crs_app = typer.Typer(no_args_is_help=True, help="Coordinate reference systems and the NTv2 grid")
app.add_typer(crs_app, name="crs")
index_app = typer.Typer(no_args_is_help=True, help="Provincial layers and indexes")
app.add_typer(index_app, name="index")
select_app = typer.Typer(no_args_is_help=True, help="Choose the files to read (12, 4 per era)")
app.add_typer(select_app, name="select")
run_app = typer.Typer(no_args_is_help=True, help="Run a phase end to end (idempotent)")
app.add_typer(run_app, name="run")
replay_app = typer.Typer(no_args_is_help=True, help="Recorded model calls (strict replay)")
app.add_typer(replay_app, name="replay")
store_app = typer.Typer(help="The tiered DuckDB store.")
app.add_typer(store_app, name="store")
prospect_app = typer.Typer(help="The cell grid, its features, and what they can support.")
app.add_typer(prospect_app, name="prospect")
openai_app = typer.Typer(no_args_is_help=True, help="The chat agent's OpenAI backend, and what it has spent.")
app.add_typer(openai_app, name="openai")


@openai_app.command("models")
def openai_models_cmd(filter: str = typer.Option("", "--filter", help="substring to narrow the list")) -> None:
    """List the models this key can reach. Costs nothing: run it before setting OPENAI_MODEL."""
    from .backends.openai_api import list_models, model_name

    names = [m for m in list_models() if filter.lower() in m.lower()]
    for name in names:
        typer.echo(f"  {name}{'   <- OPENAI_MODEL' if name == model_name() else ''}")
    typer.echo(f"{len(names)} model(s); .env asks for {model_name()!r}")


@openai_app.command("budget")
def openai_budget_cmd() -> None:
    """What the chat agent has spent so far, and what is left before calls are refused."""
    from .backends.openai_api import load_dotenv
    from .backends.spend import cap_usd, ledger_path, spent_usd

    load_dotenv()
    spent, cap = spent_usd(), cap_usd()
    typer.echo(f"  spent   ${spent:.4f}")
    typer.echo(f"  ceiling ${cap:.2f}   (OPENAI_MAX_SPEND_USD)")
    typer.echo(f"  left    ${cap - spent:.4f}")
    typer.echo(f"  ledger  {ledger_path()}")
    typer.echo("  tokens are measured; dollars are computed from the prices in .env")


@app.command()
def doctor() -> None:
    """Check the environment (read-only)."""
    failed = False
    for c in run_checks():
        mark = "ok  " if c.ok else ("FAIL" if c.required else "warn")
        typer.echo(f"[{mark}] {c.name}: {c.detail}")
        failed |= (not c.ok) and c.required
    raise typer.Exit(1 if failed else 0)


@crs_app.command("fetch-grid")
def crs_fetch_grid() -> None:
    """Download the NRCan NTv2_0 grid and pin its sha256 in grids.lock."""
    path = crs.fetch_grid()
    tr = crs.Nad27ToNad83()
    s = tr.shift(crs.CANARY["lon"], crs.CANARY["lat"])
    typer.echo(f"grid {path} sha256 {tr.grid_sha256[:12]}..., canary shift {s.dist_m:.2f} m")


@crs_app.command("shift-grid")
def crs_shift_grid() -> None:
    """Write the NAD27 -> NAD83 shift-vector grid for the web app's datum lens."""
    tr = crs.Nad27ToNad83()
    grid = crs.shift_grid(tr)
    out = PATHS.web_data / "context" / "datum_grid.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(grid, separators=(",", ":")) + "\n")
    checks = ", ".join(f"{c['label']}: {c['computed_m']} m" for c in grid["checks"])
    typer.echo(f"wrote {out} ({out.stat().st_size / 1024:.0f} KB); range {grid['stats']['min_m']} to "
               f"{grid['stats']['max_m']} m; {checks}")


@index_app.command("pull")
def index_pull(only: list[str] = typer.Option(None, "--only", help="layer keys to pull")) -> None:
    """Pull provincial layers (paced, cached, single-clause queries) into data/index."""
    from .index import pull_all

    pull_all(only or None, log=typer.echo)


@select_app.command("shortlist")
def select_shortlist() -> None:
    """Rank the uranium-tagged files from data/index and shortlist about 10 per era (no network)."""
    from .select import stage_shortlist

    stage_shortlist(log=typer.echo)


@select_app.command("probe")
def select_probe() -> None:
    """Query GeoDS table 23 for the shortlisted files only (paced, cached) and apply the size filters."""
    from .select import stage_probe

    stage_probe(log=typer.echo)


@select_app.command("final")
def select_final() -> None:
    """Pick the 12 files, record which constraints were met, and propose the split and Phase 1 subset."""
    from .select import stage_final

    stage_final(log=typer.echo)


@select_app.command("check")
def select_check() -> None:
    """Re-run the post-fetch check (scanned count, datum signals) from what is already on disk."""
    from .route import ocr_datum_hits
    from .select import stage_post_fetch

    stage_post_fetch(log=typer.echo, ocr_hits=ocr_datum_hits() or None)


@app.command("fetch")
def fetch_cmd(
    phase1: bool = typer.Option(False, "--phase1", help="only the Phase 1 dev files"),
    all_selected: bool = typer.Option(False, "--all-selected", help="all 12 selected files (the default)"),
    only: list[str] = typer.Option(None, "--only", help="file numbers to fetch (repeatable)"),
    check: bool = typer.Option(True, help="run the post-fetch scanned and datum check afterwards"),
) -> None:
    """Download report, appendix, assay and certificate files for the selected files (resumable, paced)."""
    from .fetch import stage_fetch
    from .select import stage_post_fetch

    if phase1 and all_selected:
        raise typer.BadParameter("--phase1 and --all-selected are mutually exclusive")
    res = stage_fetch(phase1=phase1, only=list(only or []), log=typer.echo)
    mb = sum(r["bytes"] for r in res) / 1e6
    typer.echo(f"{len(res)} objects, {mb:.1f} MB on disk "
               f"({sum(1 for r in res if r['status'] == 'downloaded')} downloaded this run)")
    if check:
        stage_post_fetch(log=typer.echo)


@app.command("export-layers")
def export_layers_cmd() -> None:
    """Write the evidence layers (conductors, faults, host, geochemistry, surveys) for the map."""
    from .export_layers import build

    build(log=typer.echo)


@app.command("enable-files")
def enable_files_cmd(
    files: list[str] = typer.Argument(..., help="assessment file numbers to enable (e.g. MAW00509 74K-0018)"),
    reason: str = typer.Option("enabled cell", "--reason", help="why these files are read (recorded)"),
    max_item_mb: float = typer.Option(60.0, "--max-item-mb", help="skip listed objects larger than this"),
) -> None:
    """Register files outside the original shortlist as dev files: probe their listing, never a held-out one."""
    from .select import stage_enable

    out = stage_enable(list(files), reason=reason, log=typer.echo, max_item_mb=max_item_mb)
    typer.echo(f"enabled {len(out)} file(s); next: lr fetch --only <num>, then render, ocr, route, extract --dry-run")


@app.command("lock-heldout")
def lock_heldout_cmd() -> None:
    """Write gold/heldout.lock (file numbers, report PDF sha256s, seed, timestamp). Never overwrites."""
    from .select import stage_lock

    stage_lock(log=typer.echo)


@app.command("render")
def render_cmd(workers: int = typer.Option(4, help="parallel pdftoppm processes")) -> None:
    """Render the Phase 1 dev files to 200 dpi grey PNGs plus thumbnails; never held-out PDFs."""
    from .render import stage_render

    stage_render(log=typer.echo, workers=workers)


@app.command("ocr")
def ocr_cmd(workers: int = typer.Option(4, help="parallel OCR processes")) -> None:
    """Apple Vision OCR (livetext tokens, vision lines) per rendered page, with text-layer comparison."""
    from .ocr import stage_ocr

    res = stage_ocr(log=typer.echo, workers=workers)
    typer.echo(json.dumps(res))


@app.command("route")
def route_cmd() -> None:
    """Classify rendered pages from OCR words and write data/out/routing_report.json."""
    from .route import stage_route

    stage_route(log=typer.echo)


@run_app.command("phase1")
def run_phase1(
    workers: int = typer.Option(4, help="parallel render and OCR workers"),
    fetch_all: bool = typer.Option(True, help="fetch all 12 selected files (held-out ones are hashed, never read)"),
) -> None:
    """select -> fetch -> lock -> render -> ocr -> route. Idempotent: finished stages are skipped."""
    from .fetch import stage_fetch
    from .ocr import stage_ocr
    from .render import stage_render
    from .route import ocr_datum_hits, stage_route
    from .select import stage_final, stage_lock, stage_post_fetch, stage_probe, stage_shortlist

    steps: list[tuple[str, object]] = [
        ("select shortlist", lambda: stage_shortlist(log=typer.echo)),
        ("select probe", lambda: stage_probe(log=typer.echo)),
        ("select final", lambda: stage_final(log=typer.echo)),
        ("fetch", lambda: stage_fetch(phase1=not fetch_all, log=typer.echo)),
        ("post-fetch check", lambda: stage_post_fetch(log=typer.echo)),
        ("lock-heldout", lambda: stage_lock(log=typer.echo)),
        ("render", lambda: stage_render(log=typer.echo, workers=workers)),
        ("ocr", lambda: stage_ocr(log=typer.echo, workers=workers)),
        ("route", lambda: stage_route(log=typer.echo)),
        ("post-fetch check (with OCR datum hits)",
         lambda: stage_post_fetch(log=typer.echo, ocr_hits=ocr_datum_hits() or None)),
    ]
    for name, fn in steps:
        typer.echo(f"\n=== {name} ===")
        t0 = time.monotonic()
        fn()
        typer.echo(f"--- {name}: {time.monotonic() - t0:.1f} s")


@app.command("extract")
def extract_cmd(
    config: str = typer.Option("phase2", "--config", help="configs/<id>.toml"),
    split: str = typer.Option("dev", "--split", help="dev or heldout"),
    files: list[str] = typer.Option(None, "--files", help="file numbers (repeatable)"),
    max_calls: int = typer.Option(120, "--max-calls", help="hard cap on model calls this invocation"),
    pages: int = typer.Option(None, "--pages", help="cap on planned pages"),
    max_pages_per_file: int = typer.Option(None, "--max-pages-per-file", help="override the config"),
    dry_run: bool = typer.Option(False, "--dry-run", help="print the plan and the estimate, call nothing"),
    unlock_heldout: bool = typer.Option(False, "--unlock-heldout", help="required for --split heldout"),
    replay: bool = typer.Option(False, "--replay", help="strict replay: recorded calls only, never live"),
) -> None:
    """Extract routed pages with the configured model, one page per call. Exits 75 on a usage limit."""
    from .extract import stage_extract

    backend = None
    if replay:
        from .backends.cache import CachedBackend
        from .backends.replay import ReplayBackend

        backend = CachedBackend(ReplayBackend())
    try:
        summary = stage_extract(config_id=config, split=split, files=list(files or []), max_calls=max_calls,
                                pages_limit=pages, max_pages_per_file=max_pages_per_file, dry_run=dry_run,
                                unlock_heldout=unlock_heldout, backend=backend, log=typer.echo)
    except PermissionError as e:
        typer.echo(f"refused: {e}")
        raise typer.Exit(2) from e
    if summary.get("usage_limit"):
        raise typer.Exit(75)
    if summary.get("circuit_broken"):
        raise typer.Exit(70)


@app.command("locate")
def locate_cmd(files: list[str] = typer.Option(None, "--files")) -> None:
    """Re-locate every quote against the OCR words and report how well it went.

    Locating runs inside `lr assemble` (a value and its box are made together); this command re-runs it
    and prints the breakdown the eval cares about: how many quotes were found, by what method, and
    whether the two readers agree on the digits.
    """
    from collections import Counter

    from .assemble import assembled_files, read_assembled, stage_assemble

    only = list(files or []) or None
    stage_assemble(files=only, log=lambda *a: None)
    for file_num in assembled_files():
        if only and file_num not in only:
            continue
        doc = read_assembled(file_num)
        methods: Counter = Counter()
        digits: Counter = Counter()
        printed = located = boxed_empty = 0
        for vid, v in doc["values"].items():
            if v["kind"] != "extracted":
                continue
            m = doc["value_meta"].get(vid, {})
            methods[(m.get("locate") or {}).get("locate_method")] += 1
            if m.get("printed") == "printed":
                printed += 1
                located += 1 if v["lineage"]["quote_located"] else 0
                digits[m.get("digit_agreement")] += 1
            elif v["lineage"].get("bbox"):
                boxed_empty += 1
        share = f"{100 * located / printed:.0f}%" if printed else "n/a"
        typer.echo(f"{file_num}: {located}/{printed} printed values with a located quote ({share}); "
                   f"{boxed_empty} not-printed cells given a box from their column header")
        typer.echo(f"  methods: {dict(methods)}")
        typer.echo(f"  digit agreement: {dict(digits)}")


@app.command("assemble")
def assemble_cmd(files: list[str] = typer.Option(None, "--files")) -> None:
    """Wire rows -> located values -> Collar / LithInterval / AssayInterval records."""
    from .assemble import stage_assemble

    stage_assemble(files=list(files or []) or None, log=typer.echo)


@app.command("validate")
def validate_cmd(
    files: list[str] = typer.Option(None, "--files"),
    mode: str = typer.Option("enforce", "--mode", help="enforce or shadow (config A shadows V09)"),
) -> None:
    """Run V01 to V20. Findings flag values; nothing is ever dropped."""
    from .validators import stage_validate

    stage_validate(files=list(files or []) or None, mode=mode, log=typer.echo)


@crs_app.command("transform")
def crs_transform(files: list[str] = typer.Option(None, "--files")) -> None:
    """Place each extracted collar, or refuse to and record why."""
    from .position import stage_position

    stage_position(files=list(files or []) or None, log=typer.echo)


@app.command("crosscheck")
def crosscheck_cmd(
    files: list[str] = typer.Option(None, "--files"),
    fetch_lith: bool = typer.Option(True, "--fetch-lith/--no-fetch-lith",
                                    help="fetch GeoDS lithology for matched hole names (network)"),
) -> None:
    """Match extracted collars to GeoDS and the compilation; build the adjudication queue."""
    from .crosscheck import stage_crosscheck

    stage_crosscheck(files=list(files or []) or None, fetch_lith=fetch_lith, log=typer.echo)


@prospect_app.command("inventory")
def prospect_inventory_cmd(verbose: bool = typer.Option(False, "--verbose")) -> None:
    """List the data sources this project may use, what each bears on, and what is missing."""
    from .prospect.inventory import load, summary

    inv = load()
    s = summary(inv)
    typer.echo(f"{s['sources']} sources: {s['features']} features ({s['features_verified']} verified), "
               f"{s['labels']} label, {s['context']} context; {s['redistributable']} redistributable")
    for src in inv.sources:
        mark = " " if src.verified else "?"
        count = f"{src.record_count:,}" if src.record_count else ("raster" if src.access == "stac" else "-")
        typer.echo(f" {mark} {src.key:26} {src.role:8} {src.bears_on:10} {count:>9}  {src.licence.name[:38]}")
        if verbose:
            for c in src.caveats:
                typer.echo(f"     - {c}")
    typer.echo(f"\n{len(inv.gaps)} gap(s): what does not exist publicly")
    for g in inv.gaps:
        typer.echo(f"   {g.key:26} {g.status}")
        if verbose:
            typer.echo(f"     {g.why_it_matters}")


@prospect_app.command("grid")
def prospect_grid_cmd(
    cell_m: int = typer.Option(2000, "--cell-m", help="cell size in metres"),
    buffer_m: int = typer.Option(30000, "--buffer-m", help="how far outside the basin outline to reach"),
) -> None:
    """Build the analysis grid over the Athabasca Basin and its margins."""
    from .prospect.grid import GridSpec, build

    build(GridSpec(cell_m=cell_m, buffer_m=buffer_m), log=typer.echo)


@prospect_app.command("features")
def prospect_features_cmd(
    only: list[str] = typer.Option(None, "--only", help="feature keys to rebuild"),
) -> None:
    """Compute per-cell features, each with the coverage that produced it."""
    from .prospect.features import build

    build(only=list(only) if only else None, log=typer.echo)


@prospect_app.command("rasters")
def prospect_rasters_cmd(
    start: str = typer.Option("2023-06-15", "--start"),
    end: str = typer.Option("2024-09-20", "--end"),
    max_cloud: float = typer.Option(15.0, "--max-cloud", help="scene cloud cover limit, percent"),
    only: str = typer.Option(None, "--only", help="s2 or dem, to rebuild one half"),
) -> None:
    """Per-cell water, vegetation, bare ground and terrain, read from public COGs."""
    from .prospect.rasters import build

    build(start=start, end=end, max_cloud=max_cloud, only=only, log=typer.echo)


@prospect_app.command("corpus")
def prospect_corpus_cmd(
    files: int = typer.Option(60, "--files", help="how many files to download and index as page text"),
    region_only: bool = typer.Option(True, "--region-only/--province", help="limit to the study area"),
    download: bool = typer.Option(True, "--download/--on-disk-only",
                                  help="fetch more files, or index only what is already here"),
) -> None:
    """Index the assessment corpus: every linked file by metadata, and page text for the documents we hold."""
    from .prospect.corpus import build
    from .prospect.grid import load_cells
    from .prospect.rasters import region_bbox

    bbox = region_bbox(load_cells()) if region_only else None
    build(n_files=files, bbox=bbox, download=download, log=typer.echo)


@prospect_app.command("retrieve")
def prospect_retrieve_cmd(
    query: str = typer.Argument(None, help="what to look for"),
    lon: float = typer.Option(None, "--lon"),
    lat: float = typer.Option(None, "--lat"),
    radius_km: float = typer.Option(40.0, "--radius-km"),
    k: int = typer.Option(8, "-k"),
    stats: bool = typer.Option(False, "--stats", help="report what the corpus holds and stop"),
) -> None:
    """Search the assessment corpus: spatial filter first, then rank."""
    from .prospect.retrieve import retrieve, summary

    if stats or not query:
        for key, value in summary().items():
            typer.echo(f"  {key:18} {value:,}")
        return
    for p in retrieve(query, lon=lon, lat=lat, radius_km=radius_km, k=k):
        where = f" {p.distance_km:.0f} km" if p.distance_km == p.distance_km and p.distance_km is not None else ""
        typer.echo(f"\n[{p.tier}] {p.cite()}{where}  score {p.score}")
        typer.echo(f"  {p.text[:400]}")


@prospect_app.command("labels")
def prospect_labels_cmd() -> None:
    """Mark positive cells, group them into camps, and assign spatial folds."""
    from .prospect.labels import build

    build(log=typer.echo)


@prospect_app.command("score")
def prospect_score_cmd(
    model: str = typer.Option("all", "--model", help="criteria | learned | effort | all"),
) -> None:
    """Score every cell: the knowledge-driven criteria score, with each criterion's contribution kept."""
    if model in ("criteria", "all"):
        from .prospect.criteria import score

        score(log=typer.echo)
    if model in ("learned", "effort", "all"):
        from .prospect.models import run

        run(log=typer.echo)
    if model not in ("criteria", "learned", "effort", "all"):
        raise typer.BadParameter("model must be criteria, learned, effort or all")


@prospect_app.command("memo")
def prospect_memo_cmd(
    cell: str = typer.Option(None, "--cell", help="cell id; omitted, the highest-scoring cell is used"),
    model: str = typer.Option("claude-sonnet-5", "--model"),
    effort: str = typer.Option("medium", "--effort"),
    budget: float = typer.Option(1.20, "--max-budget-usd", help="per model call"),
    mode: str = typer.Option("panel", "--mode", help="panel (agent picks its tools) or oneshot (tools "
                                                     "precomputed, one call per role)"),
    roles: str = typer.Option("", "--roles", help="comma-separated subset, e.g. adjudicator"),
) -> None:
    """Run the proponent, skeptic and adjudicator over one cell, with the fabrication gate."""
    from .backends.cache import CachedBackend
    from .backends.claude_cli import ClaudeCliBackend
    from .prospect.memo import run_panel
    from .store import connect

    if not cell:
        con = connect(read_only=True)
        try:
            cell = con.execute(
                "select cell_id from derived.cell_score where model = 'criteria' and score is not null "
                "order by score desc limit 1"
            ).fetchone()[0]
        finally:
            con.close()
    backend = CachedBackend(ClaudeCliBackend(timeout_s=600, max_budget_usd=budget))
    from .prospect.memo import ROLES

    chosen = tuple(r.strip() for r in roles.split(",") if r.strip()) or ROLES
    out = run_panel(cell, backend, model=model, effort=effort, mode=mode, roles=chosen, log=typer.echo)
    for role, r in out["results"].items():
        memo = r.get("memo") or {}
        typer.echo(f"\n=== {role}: {memo.get('verdict') or '(no memo)'}"
                   f"{'' if r['published'] else '  [REJECTED]'}")
        if memo.get("summary"):
            typer.echo(f"  {memo['summary']}")
        for claim in (memo.get("claims") or [])[:6]:
            typer.echo(f"  - {claim.get('text')}")
        if memo.get("next_observation"):
            typer.echo(f"  next: {memo['next_observation']}")
        for problem in r.get("problems") or []:
            typer.echo(f"  ! {problem}")


@prospect_app.command("serve")
def prospect_serve_cmd(
    port: int = typer.Option(8787, "--port"),
    model: str = typer.Option("", "--model", help="default: OPENAI_MODEL for openai, sonnet for claude"),
    effort: str = typer.Option("medium", "--effort"),
    backend: str = typer.Option("openai", "--backend", help="openai | claude"),
) -> None:
    """Serve the evidence record and the per-cell conversation to the web app, on localhost."""
    from .prospect.serve import serve

    serve(port=port, model=model, effort=effort, backend=backend, log=typer.echo)


@prospect_app.command("record-chat")
def prospect_record_chat_cmd(
    cell: str = typer.Option("0201_0072", "--cell"),
    model: str = typer.Option("", "--model"),
    effort: str = typer.Option("medium", "--effort"),
    backend: str = typer.Option("openai", "--backend", help="openai | claude"),
) -> None:
    """Ask the tour's questions for real and write the gate-passed answers for the walkthrough to replay."""
    from .prospect.serve import make_backend
    from .prospect.recorded import record, write

    chosen, model = make_backend(backend, model)
    payload = record(cell, chosen, model=model, effort=effort, log=typer.echo)
    path = write(payload)
    kept = sum(1 for t in payload["turns"] if t["published"])
    typer.echo(f"  {kept} of {len(payload['turns'])} answers passed the gate; "
               f"{len(payload['values'])} values cited; ${payload['cost_usd']}")
    typer.echo(f"  wrote {path}")


@prospect_app.command("gate-eval")
def prospect_gate_eval_cmd(
    cells: str = typer.Option("", "--cells", help="comma-separated cell ids; default: the assessed ones"),
    seed: int = typer.Option(20260918, "--seed"),
    per_cell: int = typer.Option(8, "--per-cell", help="values sampled per cell"),
    out: str = typer.Option("data/exports/gate_eval.json", "--out"),
) -> None:
    """Put honest and corrupted claims to the fabrication gate and report what it sorts correctly."""
    import json as _json

    from .prospect.gate_eval import run
    from .store import connect

    from pathlib import Path as _Path

    path = _Path(out)
    ids = [c.strip() for c in cells.split(",") if c.strip()]
    if not ids:
        con = connect(read_only=True)
        try:
            ids = [r[0] for r in con.execute("select distinct cell_id from agent.memo order by 1").fetchall()]
        finally:
            con.close()
    if not ids:
        raise typer.BadParameter("no cells assessed yet; pass --cells")
    typer.echo(f"gate eval over {len(ids)} cell(s)")
    report = run(ids, seed=seed, per_cell=per_cell, log=typer.echo)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_json.dumps(report, indent=2) + "\n")
    typer.echo(f"  wrote {path}")


@prospect_app.command("table")
def prospect_table_cmd() -> None:
    """Write knowledge/data_readiness_table.md: sources, processing, features, imagery and gaps."""
    from .prospect.table import build

    build(log=typer.echo)


@prospect_app.command("readiness")
def prospect_readiness_cmd() -> None:
    """Report how much of the basin each feature actually covers, before anything is scored."""
    from .prospect.readiness import report

    report(log=typer.echo)


@prospect_app.command("export")
def prospect_export_cmd() -> None:
    """Write the readiness scorecard and the coverage layer into web/public/data/prospect."""
    from .prospect.export import build

    build(log=typer.echo)


@store_app.command("rebuild")
def store_rebuild_cmd() -> None:
    """Flatten every stage into the tiered data/lr.duckdb plus the two GeoParquet layers."""
    from .store import rebuild

    rebuild(log=typer.echo)


@store_app.command("audit")
def store_audit_cmd() -> None:
    """Check that no table mixes provenance tiers (native, read, derived, agent)."""
    from .store import connect, tier_audit

    con = connect()
    try:
        problems = tier_audit(con)
    finally:
        con.close()
    for line in problems:
        typer.echo(f"  {line}")
    typer.echo("tier audit clean" if not problems else f"{len(problems)} problem(s)")
    raise typer.Exit(0 if not problems else 1)


@app.command("export-reports")
def export_reports_cmd(
    files: list[str] = typer.Option(None, "--files"),
    public_safe: bool = typer.Option(False, "--public-safe", help="no page images; image and thumb null"),
) -> None:
    """Write reports/index.json, reports/<file>/{report,pages}.json and the page images."""
    from .export_reports import stage_export_reports

    res = stage_export_reports(files=list(files or []) or None, public_safe=public_safe, log=typer.echo)
    if res["contract_errors"]:
        raise typer.Exit(1)


@replay_app.command("pack")
def replay_pack(run: str = typer.Option(..., "--run", help="run id under data/runs")) -> None:
    """Bundle a run's recorded envelopes into tests/fixtures/replay/."""
    from .backends.replay import pack_run

    written = pack_run(run)
    typer.echo(f"packed {len(written)} call record(s) into tests/fixtures/replay/")


@run_app.command("phase2")
def run_phase2(
    config: str = typer.Option("phase2", "--config"),
    files: list[str] = typer.Option(None, "--files"),
    max_calls: int = typer.Option(120, "--max-calls"),
    fetch_lith: bool = typer.Option(True, "--fetch-lith/--no-fetch-lith"),
    public_safe: bool = typer.Option(False, "--public-safe"),
) -> None:
    """extract -> assemble (locate) -> validate -> crs -> crosscheck -> store -> export-reports."""
    from .assemble import stage_assemble
    from .crosscheck import stage_crosscheck
    from .export_reports import stage_export_reports
    from .extract import load_config, stage_extract
    from .position import stage_position
    from .store import stage_store
    from .validators import stage_validate

    only = list(files or []) or None
    cfg = load_config(config)
    steps: list[tuple[str, object]] = [
        ("extract", lambda: stage_extract(config_id=config, files=only or [], max_calls=max_calls,
                                          log=typer.echo)),
        ("assemble (locate)", lambda: stage_assemble(files=only, log=typer.echo)),
        ("validate", lambda: stage_validate(files=only, mode=cfg.validator_mode, log=typer.echo)),
        ("crs transform", lambda: stage_position(files=only, log=typer.echo)),
        ("crosscheck", lambda: stage_crosscheck(files=only, fetch_lith=fetch_lith, log=typer.echo)),
        ("validate (with positions and matches)",
         lambda: stage_validate(files=only, mode=cfg.validator_mode, log=typer.echo)),
        ("store", lambda: stage_store(log=typer.echo)),
        ("export-reports", lambda: stage_export_reports(files=only, public_safe=public_safe, log=typer.echo)),
    ]
    for name, fn in steps:
        typer.echo(f"\n=== {name} ===")
        t0 = time.monotonic()
        fn()
        typer.echo(f"--- {name}: {time.monotonic() - t0:.1f} s")


@app.command("export-web")
def export_web_cmd(public_safe: bool = typer.Option(False, "--public-safe", help="omit non-redistributable data")) -> None:
    """Write the web data contract into web/public/data."""
    from .export_web import export

    export(public_safe=public_safe, log=typer.echo)


@app.command("export-eval")
def export_eval_cmd() -> None:
    """Write run statistics for the eval page (not accuracy: no gold labels yet)."""
    from .export_eval import export

    export(log=typer.echo)


@app.command("probe-backend")
def probe_backend(
    pdf: str = typer.Option("data/probe/drilllog_1980s.pdf", help="PDF to probe"),
    page: int = typer.Option(1, help="1-based page number"),
    model: str = typer.Option("claude-sonnet-5", help="full model id"),
    effort: str = typer.Option("medium"),
) -> None:
    """One real claude -p call on a rendered page; saves the envelope as a replay fixture."""
    from pathlib import Path

    from .probe import run_probe

    rec = run_probe(Path(pdf), page, model, effort)
    r = rec["response"]
    typer.echo(json.dumps({k: r[k] for k in ("model_resolved", "num_turns", "duration_s", "cost_usd")}, indent=2))
    typer.echo("usage: " + json.dumps(r["usage"]))
    typer.echo("structured output:\n" + json.dumps(r["structured"], indent=2))
    typer.echo(f"fixture: {rec['fixture_path']}")


if __name__ == "__main__":
    app()
