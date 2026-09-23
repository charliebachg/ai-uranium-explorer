import { Bot, X } from "lucide-react";
import { useEffect, useMemo, useState, useSyncExternalStore } from "react";
import { V } from "@/components/values/V";
import type { Candidate } from "@/data/contract";
import { hasValue, registryVersion, subscribeRegistry } from "@/data/registry";
import { ChatPanel } from "@/features/prospect/ChatPanel";
import {
  candidateScoreId,
  mapKnownShareId,
  mapScoreId,
  registerMapCell,
} from "@/features/prospect/cellValues";
import { EvidencePanel } from "@/features/prospect/EvidencePanel";
import type { ServiceState } from "@/features/prospect/OfflineNotice";
import { serviceState, useCandidates, useEvidence, useServiceHealth } from "@/features/prospect/queries";
import { cn } from "@/lib/cn";
import { formatLatLon } from "@/lib/format";
import { useStore } from "@/state/store";
import { JobsStrip } from "./JobsStrip";

/**
 * The agent, on the right of the map. Click a cell and this holds everything the system knows about it: the
 * evidence record the scores were computed from, and a conversation that can only draw on that same record.
 *
 * It is one rail rather than a separate page because the question a visitor actually asks is "what is going on
 * *there*", and answering it on a different screen from the map breaks the thread. The evidence and the chat
 * read the same tool calls, so the map, the panel and the answer cannot disagree.
 */

const CANDIDATE_LIMIT = 12;

export function AgentRail() {
  const selected = useStore((s) => s.selected);
  const report = useStore((s) => s.report);
  const scoresOn = useStore((s) => s.visible.prospect);
  const select = useStore((s) => s.select);
  const toggleLayer = useStore((s) => s.toggleLayer);

  const [tab, setTab] = useState<"evidence" | "chat">("evidence");
  // server state through TanStack Query: probed, cached, refetched on its own schedule; never retried silently
  const healthQ = useServiceHealth();
  const service = serviceState(healthQ);
  const rankedQ = useCandidates(CANDIDATE_LIMIT, service === "up");
  const ranked: Candidate[] = rankedQ.data ?? [];
  // the ranked list is the only place the rail knows a cell's position without the map
  const byCell = useMemo(
    () => new Map(ranked.map((c) => [c.cell_id, [c.lon, c.lat] as [number, number]])),
    [ranked],
  );

  const cellId = selected?.dataset === "cell" ? String(selected.props.cid ?? "") : null;
  const evidenceQ = useEvidence(cellId, service === "up");
  const record = evidenceQ.data ?? null;
  const loading = evidenceQ.isFetching && !evidenceQ.data;
  const error = evidenceQ.error ? String(evidenceQ.error) : null;

  const replay = useStore((s) => s.chatReplay);

  // the walkthrough opens a conversation, so it should land on the conversation rather than behind a tab
  useEffect(() => {
    if (replay && replay.cellId === cellId) setTab("chat");
  }, [replay, cellId]);

  // the exported row is what the map drew; registering it keeps the rail truthful with the service down
  useEffect(() => {
    if (cellId && selected) registerMapCell(cellId, selected.props);
  }, [cellId, selected]);

  // the report panel owns this side of the screen when a file is open; two rails would fight for it
  if (report) return null;

  return (
    <aside
      className={cn(
        "glass pointer-events-auto flex min-h-0 w-[420px] flex-col overflow-hidden rounded-2xl shadow-2xl shadow-black/40",
        // once a cell is open the rail is the working surface and takes the column; empty, it stays a card
        cellId && "flex-1",
      )}
      aria-label="Agent"
      data-testid="agent-rail"
    >
      <header className="flex shrink-0 items-center gap-2 border-line border-b px-4 py-2.5">
        <Bot className="size-4 text-ink-3" aria-hidden="true" />
        <span className="font-medium text-[11px] text-ink-3 uppercase tracking-[0.14em]">Agent</span>
        {cellId ? (
          <>
            <span className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[11px] text-ink" data-ident>
              {cellId}
            </span>
            <span className="tabular text-[11px] text-ink-3" data-instrument="cell-centre">
              {formatLatLon(selected?.lngLat[0] ?? 0, selected?.lngLat[1] ?? 0)}
            </span>
            <button
              type="button"
              onClick={() => select(null)}
              className="ml-auto rounded-md p-1 text-ink-3 hover:bg-white/5 hover:text-ink"
              aria-label="Clear the selected cell"
            >
              <X className="size-3.5" />
            </button>
          </>
        ) : (
          <span className="text-[11.5px] text-ink-3">no cell selected</span>
        )}
      </header>

      {!cellId ? (
        <Empty
          scoresOn={!!scoresOn}
          onShowScores={() => toggleLayer("prospect", true)}
          ranked={ranked}
          service={service}
        />
      ) : (
        <div className="flex min-h-0 flex-1 flex-col">
          <MapScores cellId={cellId} />
          {/* the cell's background jobs (an analyst run in progress, what the last one produced) */}
          <JobsStrip key={cellId} cellId={cellId} state={service} />
          {/* two views of one cell: what is recorded about it, and a conversation about that record */}
          <div className="flex shrink-0 gap-1 border-line border-b px-3 pt-2" role="tablist">
            {(["evidence", "chat"] as const).map((id) => (
              <button
                key={id}
                type="button"
                role="tab"
                aria-selected={tab === id}
                data-testid={`tab-${id}`}
                onClick={() => setTab(id)}
                className={cn(
                  "-mb-px rounded-t-lg border-b-2 px-3 py-1.5 text-[12px] capitalize transition-colors",
                  tab === id ? "border-focus/70 text-ink" : "border-transparent text-ink-3 hover:text-ink-2",
                )}
              >
                {id}
              </button>
            ))}
          </div>
          {tab === "evidence" ? (
            <div className="min-h-0 flex-1 overflow-y-auto px-3 py-2" data-testid="tab-panel-evidence">
              <EvidencePanel
                cellId={cellId}
                state={service}
                record={record}
                error={error}
                loading={loading}
              />
            </div>
          ) : (
            <div className="min-h-0 flex-1" data-testid="tab-panel-chat">
              {/* a citation is a pointer at something: clicking it opens that thing rather than just
                  highlighting itself. Another cell selects it on the map; anything else goes to the record. */}
              <ChatPanel
                cellId={cellId}
                state={service}
                onCite={(_id, citedCell) => {
                  if (citedCell && citedCell !== cellId) {
                    const at = byCell.get(citedCell);
                    if (at) select({ dataset: "cell", id: -1, props: { cid: citedCell }, lngLat: at });
                    return;
                  }
                  setTab("evidence");
                }}
              />
            </div>
          )}
        </div>
      )}
    </aside>
  );
}

/** The three scores as the static export has them, so the rail says something before the service answers. */
function MapScores({ cellId }: { cellId: string }) {
  // the row is registered in an effect after the first render, so this has to watch the registry itself:
  // without it the strip decides there is nothing to show and never looks again
  useSyncExternalStore(subscribeRegistry, registryVersion);
  const model = useStore((s) => s.scoreModel);
  // a cell picked from the ranked list carries no exported row; the evidence table below is then the source
  if (!(["criteria", "learned", "effort"] as const).some((m) => hasValue(mapScoreId(cellId, m)))) return null;
  return (
    <div
      className="flex shrink-0 flex-wrap items-baseline gap-x-3 gap-y-1 border-line border-b px-4 py-2 text-[11.5px] text-ink-3"
      data-testid="map-scores"
    >
      {(["criteria", "learned", "effort"] as const).map((m) => {
        const id = mapScoreId(cellId, m);
        return (
          <span key={m} className={cn(model === m && "text-ink-2")}>
            {/* a cell this model could not score has no score, which is not a low one */}
            {m} <V id={hasValue(id) ? id : null} className="text-ink-2" emptyText="no score" />
          </span>
        );
      })}
      <span className="ml-auto">
        known{" "}
        <V
          id={hasValue(mapKnownShareId(cellId)) ? mapKnownShareId(cellId) : null}
          className="text-ink-2"
          emptyText="—"
        />
      </span>
    </div>
  );
}

/** Nothing selected yet: say what to click, and offer the ranked cells as the other way in. */
function Empty({
  scoresOn,
  onShowScores,
  ranked,
  service,
}: {
  scoresOn: boolean;
  onShowScores: () => void;
  ranked: Candidate[];
  service: ServiceState;
}) {
  const select = useStore((s) => s.select);
  return (
    <div className="min-h-0 flex-1 overflow-y-auto px-4 py-3" data-testid="agent-empty">
      <p className="text-[12.5px] text-ink-2">Click a cell to see its evidence.</p>
      {!scoresOn ? (
        <button
          type="button"
          onClick={onShowScores}
          className="mt-2 rounded-lg bg-raised px-2.5 py-1.5 text-[12px] text-ink-2 hover:text-ink"
          data-testid="show-scores"
        >
          Show the score cells
        </button>
      ) : null}

      {service === "up" && ranked.length ? (
        <>
          <div className="mt-4 mb-1.5 text-[10.5px] text-ink-3 uppercase tracking-[0.12em]">
            Highest criteria score
          </div>
          <p className="mb-2 text-[11.5px] text-ink-3">
            Mostly known ground: a check on the ranking, not a shortlist.
          </p>
          <div className="flex flex-wrap gap-1.5">
            {ranked.map((c) => (
              <button
                key={c.cell_id}
                type="button"
                data-testid="ranked-cell"
                onClick={() =>
                  select({
                    dataset: "cell",
                    id: -1,
                    props: { cid: c.cell_id },
                    lngLat: [c.lon, c.lat],
                  })
                }
                className={cn(
                  "flex items-center gap-2 rounded-lg border border-line bg-black/20 px-2 py-1",
                  "text-[11.5px] text-ink-3 transition-colors hover:text-ink-2",
                )}
              >
                <span className="font-mono text-[10.5px]" data-ident>
                  {c.cell_id}
                </span>
                <V id={candidateScoreId(c.cell_id)} className="text-ink-2" />
                {c.label_name ? (
                  <span className="text-ink-3" data-source-text>
                    {c.label_name}
                  </span>
                ) : null}
              </button>
            ))}
          </div>
        </>
      ) : null}
    </div>
  );
}
