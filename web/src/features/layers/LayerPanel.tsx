import { ChevronsLeft, Layers, Moon, Scale, Sun } from "lucide-react";
import { V } from "@/components/values/V";
import { BASEMAPS } from "@/config/basemaps";
import { COLORS, type LayerGroup, type LayerGroupId, layerGroups } from "@/config/layers";
import { hasValue } from "@/data/registry";
import { DatumTools } from "@/features/datum/DatumTools";
import { cn } from "@/lib/cn";
import { type ScoreModel, useStore } from "@/state/store";
import { ReportsList } from "./ReportsList";

const GROUPS = layerGroups();
/**
 * Grouped the way MineTRACE groups its evidence: the prospectivity surface, then the geological context it is
 * computed from, then geochemistry, then the geophysics.
 *
 * Two departures, both because of what this ground actually has. Their geophysics group holds magnetics,
 * gravity and radiometrics; none of the three is published as a grid for Saskatchewan, so that group here
 * holds the gaps instead of the layers, named rather than quietly missing. And exploration effort is pulled
 * out into a group of its own, because separating "what the rock shows" from "where people already looked" is
 * the argument this whole demo makes, and the layer rail is the first place to make it.
 */
const SECTIONS: { title: string; note?: string; ids: LayerGroupId[] }[] = [
  { title: "Pathway and trap", ids: ["conductors", "faults", "host"] },
  { title: "Geochemistry", ids: ["lakeSediment", "lakeWater", "boulders"] },
  {
    title: "Where people already looked",
    note: "Effort, not rock. Under spatial folds these predict known deposits better than the geology does.",
    ids: ["compilation", "geods", "surveysAir", "surveysGround"],
  },
  { title: "Known uranium", ids: ["deposits", "occurrences"] },
  { title: "Base", ids: ["basin", "nts", "relief"] },
];

/** The geophysics MineTRACE shows and this ground does not publish. Named, because a silent omission reads
 * as "nothing there" when it means "nobody published it". */
const GEOPHYSICS_GAPS: { label: string; why: string }[] = [
  { label: "Magnetics", why: "no public grid for Saskatchewan" },
  { label: "Gravity", why: "no public grid for Saskatchewan" },
  { label: "Radiometrics", why: "no public grid for Saskatchewan" },
];

const SCORE_MODELS: { id: ScoreModel; label: string; hint: string }[] = [
  { id: "criteria", label: "Criteria", hint: "hand-written rules from the handbook" },
  { id: "learned", label: "Learned", hint: "fitted to the labelled cells" },
  { id: "effort", label: "Effort", hint: "the null model: where people already looked" },
  { id: "difference", label: "Learned − effort", hint: "what the learned score adds over effort" },
];

export function LayerPanel() {
  const visible = useStore((s) => s.visible);
  const toggle = useStore((s) => s.toggleLayer);
  const uraniumOnly = useStore((s) => s.uraniumOnly);
  const setUraniumOnly = useStore((s) => s.setUraniumOnly);
  const basemap = useStore((s) => s.basemap);
  const setBasemap = useStore((s) => s.setBasemap);
  const theme = useStore((s) => s.theme);
  const setTheme = useStore((s) => s.setTheme);
  const rail = useStore((s) => s.ui.rail);
  const setUi = useStore((s) => s.setUi);

  if (!rail) {
    return (
      <button
        type="button"
        onClick={() => setUi({ rail: true })}
        className="glass pointer-events-auto flex size-11 items-center justify-center rounded-xl text-ink-2 hover:text-ink"
        aria-label="Show layers"
      >
        <Layers className="size-4.5" />
      </button>
    );
  }

  return (
    <aside
      className="glass pointer-events-auto flex min-h-0 w-[284px] flex-col overflow-hidden rounded-2xl shadow-2xl shadow-black/40"
      aria-label="Layers"
    >
      <header className="flex items-center justify-between px-4 pt-3.5 pb-2">
        <div className="flex items-center gap-2 font-medium text-[11px] text-ink-3 uppercase tracking-[0.14em]">
          <Layers className="size-3.5" /> Layers
        </div>
        <button
          type="button"
          onClick={() => setUi({ rail: false })}
          className="rounded-md p-1 text-ink-3 hover:bg-white/5 hover:text-ink"
          aria-label="Collapse layers"
        >
          <ChevronsLeft className="size-4" />
        </button>
      </header>

      <div className="min-h-0 flex-1 overflow-y-auto px-2 pb-2">
        <ReportsList />
        <DatumTools />
        <section className="mb-1">
          <div className="px-2 pt-2 pb-1 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">Scores</div>
          <LayerRow
            group={GROUPS.find((x) => x.id === "prospect") as LayerGroup}
            on={!!visible.prospect}
            onToggle={() => toggle("prospect")}
          />
          {visible.prospect ? <ScorePicker /> : null}
        </section>
        {SECTIONS.map((section) => (
          <section key={section.title} className="mb-1">
            <div className="px-2 pt-2 pb-1 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">
              {section.title}
            </div>
            {section.note ? <p className="px-2 pb-1 text-[11px] text-ink-3">{section.note}</p> : null}
            {section.ids.map((id) => {
              const g = GROUPS.find((x) => x.id === id) as LayerGroup;
              return (
                <div key={id}>
                  <LayerRow group={g} on={!!visible[id]} onToggle={() => toggle(id)} />
                  {id === "compilation" && visible.compilation ? (
                    <button
                      type="button"
                      role="switch"
                      aria-checked={uraniumOnly}
                      onClick={() => setUraniumOnly(!uraniumOnly)}
                      className="ml-9 flex w-[calc(100%-2.25rem)] cursor-pointer items-center justify-between rounded-lg py-1.5 pr-2 pl-1 text-left text-[12px] text-ink-2 hover:bg-white/[0.03]"
                    >
                      <span>
                        Uranium-tagged only <span className="text-ink-3">(</span>
                        <V id="m:compilation_uranium" className="text-ink-3" />
                        <span className="text-ink-3">)</span>
                      </span>
                      <SwitchVisual on={uraniumOnly} />
                    </button>
                  ) : null}
                </div>
              );
            })}
          </section>
        ))}

        <section className="mb-1">
          <div className="px-2 pt-2 pb-1 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">
            Geophysics
          </div>
          <p className="px-2 pb-1 text-[11px] text-ink-3">
            The three layers a prospectivity map usually leans on. None is published as a grid here.
          </p>
          {GEOPHYSICS_GAPS.map((gap) => (
            <div
              key={gap.label}
              data-testid="layer-gap"
              className="flex items-center gap-3 rounded-xl px-2 py-1.5 opacity-45"
              title={gap.why}
            >
              <span className="size-4 shrink-0 rounded border border-line border-dashed" aria-hidden="true" />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-[13px] text-ink-2 line-through">{gap.label}</span>
                <span className="block truncate text-[11px] text-ink-3">{gap.why}</span>
              </span>
            </div>
          ))}
        </section>
      </div>

      <div className="shrink-0 border-line border-t px-4 pt-3 pb-3.5">
        <div className="mb-2 flex items-center justify-between">
          <span className="text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">Basemap</span>
          <button
            type="button"
            onClick={() => setTheme(theme === "dark" ? "light" : "dark")}
            data-testid="theme-toggle"
            aria-label={theme === "dark" ? "Switch to the light theme" : "Switch to the dark theme"}
            className="flex items-center gap-1.5 rounded-lg px-2 py-1 text-[11.5px] text-ink-3 hover:bg-white/[0.05] hover:text-ink-2"
          >
            {theme === "dark" ? <Moon className="size-3.5" /> : <Sun className="size-3.5" />}
            {theme === "dark" ? "Dark" : "Light"}
          </button>
        </div>
        <div className="grid grid-cols-2 gap-1 rounded-lg bg-black/30 p-1">
          {BASEMAPS.map((b) => (
            <button
              key={b.id}
              type="button"
              aria-pressed={basemap === b.id}
              title={b.description}
              onClick={() => setBasemap(b.id)}
              className={cn(
                "rounded-md px-2 py-1.5 text-[12px] transition-colors duration-200",
                basemap === b.id ? "bg-raised text-ink shadow-sm" : "text-ink-3 hover:text-ink-2",
              )}
            >
              {b.label}
            </button>
          ))}
        </div>
        <button
          type="button"
          onClick={() => setUi({ attribution: true })}
          className="mt-3 flex w-full items-center gap-2 rounded-lg px-2 py-1.5 text-[12px] text-ink-3 hover:bg-white/[0.04] hover:text-ink-2"
        >
          <Scale className="size-3.5" /> Data sources and licences
        </button>
      </div>
    </aside>
  );
}

/** Which score the cells are coloured by. Four buttons, because a dropdown hides the comparison. */
function ScorePicker() {
  const model = useStore((s) => s.scoreModel);
  const setModel = useStore((s) => s.setScoreModel);
  return (
    <div
      className="ml-9 mr-2 mb-1 grid grid-cols-2 gap-1 rounded-lg bg-black/30 p-1"
      data-testid="score-picker"
    >
      {SCORE_MODELS.map((m) => (
        <button
          key={m.id}
          type="button"
          aria-pressed={model === m.id}
          title={m.hint}
          data-testid={`score-model-${m.id}`}
          onClick={() => setModel(m.id)}
          className={cn(
            "rounded-md px-2 py-1 text-[11.5px] transition-colors duration-200",
            model === m.id ? "bg-raised text-ink shadow-sm" : "text-ink-3 hover:text-ink-2",
          )}
        >
          {m.label}
        </button>
      ))}
    </div>
  );
}

function LayerRow({ group, on, onToggle }: { group: LayerGroup; on: boolean; onToggle: () => void }) {
  return (
    <button
      type="button"
      role="switch"
      aria-checked={on}
      onClick={onToggle}
      className={cn(
        "group flex w-full cursor-pointer items-center gap-3 rounded-xl px-2 py-2 text-left transition-colors duration-150 hover:bg-white/[0.04]",
        !on && "opacity-60",
      )}
      title={group.detail}
    >
      <Swatch legend={group.legend} />
      <span className="min-w-0 flex-1">
        <span className="block truncate text-[13px] text-ink">{group.label}</span>
        {/* the count is registered by the manifest; during a route remount it can briefly not be there yet,
            and a missing count is not worth taking the app down for */}
        {group.countStat && hasValue(group.countStat) ? (
          <span className="block text-[11px] text-ink-3">
            <V id={group.countStat} className="text-ink-3" /> features
          </span>
        ) : (
          <span className="block truncate text-[11px] text-ink-3">{group.detail}</span>
        )}
      </span>
      <SwitchVisual on={on} />
    </button>
  );
}

function Swatch({ legend }: { legend: LayerGroup["legend"] }) {
  const c = legend.color;
  return (
    <span
      className="flex size-6 shrink-0 items-center justify-center rounded-md bg-black/30"
      aria-hidden="true"
    >
      {legend.kind === "dot" && (
        <span className="size-2 rounded-full" style={{ background: c, boxShadow: `0 0 8px ${c}` }} />
      )}
      {legend.kind === "hollow" && (
        <span className="size-2.5 rounded-full border" style={{ borderColor: c }} />
      )}
      {legend.kind === "line" && (
        <span className="h-[2px] w-3.5 rounded" style={{ background: c, boxShadow: `0 0 6px ${c}` }} />
      )}
      {legend.kind === "fill" && (
        <span className="size-3 rounded-[3px] border" style={{ borderColor: c, background: `${c}40` }} />
      )}
      {legend.kind === "grid" && (
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
          <path
            d="M1 1h12v12H1zM7 1v12M1 7h12"
            fill="none"
            stroke={COLORS.neutral}
            strokeWidth="1"
            opacity="0.7"
          />
        </svg>
      )}
      {legend.kind === "relief" && (
        <svg width="14" height="14" viewBox="0 0 14 14" aria-hidden="true">
          <path d="M1 12l4-6 3 4 2-3 3 5z" fill="#2b3848" stroke="#5b6776" strokeWidth="0.8" />
        </svg>
      )}
    </span>
  );
}

function SwitchVisual({ on }: { on: boolean }) {
  return (
    <span
      aria-hidden="true"
      className={cn(
        "relative h-[18px] w-[30px] shrink-0 rounded-full transition-colors duration-200",
        on ? "bg-[#3a4d66]" : "bg-white/10",
      )}
    >
      <span
        className={cn(
          "absolute top-[2px] size-[14px] rounded-full shadow transition-all duration-200 ease-out-expo",
          on ? "left-[14px] bg-st-pass" : "left-[2px] bg-ink-3",
        )}
      />
    </span>
  );
}
