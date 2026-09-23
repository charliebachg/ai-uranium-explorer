import { CircleDashed, CircleSlash, MapPin, ShieldQuestion } from "lucide-react";
import { useState } from "react";
import { Chip } from "@/components/ui/StatusMark";
import { Tip } from "@/components/ui/Tip";
import { V } from "@/components/values/V";
import type { AnalystChain, CellEvidence, ChainNode, ChainVerdict } from "@/data/contract";
import { hasValue } from "@/data/registry";
import { cn } from "@/lib/cn";
import { knownShareId, weightId } from "./cellValues";
import { OfflineNotice, type ServiceState } from "./OfflineNotice";

/**
 * Everything the local service holds about one cell: the three scores, what each criterion contributed, how
 * close the nearest labelled deposit is, the memos three agents wrote about it, and the chain the staged
 * analyst produced over it.
 *
 * Two distinctions carry the panel. A criterion that is unknown here is not a criterion that failed: it is
 * drawn with no bar at all and named as unmeasured, while a criterion that was measured and fell short gets a
 * bar and a number. And a memo that did not pass the evidence check is shown as rejected, with its verdict
 * struck out and its claims withheld, so it reads as a record of a failure rather than as an argument. The
 * chain section holds to both: a node's status is words, a withheld node or decision is marked and its
 * gate's objection shown, and a verdict is words in the page's plain ink, never a colour.
 */

type Row = Record<string, unknown>;

const str = (row: Row, key: string): string => (typeof row[key] === "string" ? row[key] : "");
const numberAt = (row: Row, key: string): number | null =>
  typeof row[key] === "number" ? (row[key] as number) : null;
const idAt = (row: Row, key: string): string | null => (typeof row[key] === "string" ? row[key] : null);

export function EvidencePanel({
  cellId,
  state,
  record,
  error,
  loading,
}: {
  cellId: string | null;
  state: ServiceState;
  record: CellEvidence | null;
  error: string | null;
  loading: boolean;
}) {
  return (
    <section className="glass rounded-2xl p-4" data-testid="evidence-panel">
      <header className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
        {record ? (
          <span className="text-[11px] text-ink-3">
            {record.in_basin ? "over the sandstone" : "in the buffer, outside the basin"}
          </span>
        ) : null}
      </header>

      {state !== "up" ? (
        <OfflineNotice state={state} />
      ) : !cellId ? (
        <p className="mt-3 text-[12.5px] text-ink-3">Click a cell to see its evidence.</p>
      ) : error ? (
        <p className="mt-3 text-[12.5px] text-st-miss">{error}</p>
      ) : loading || !record ? (
        <p className="mt-3 text-[12.5px] text-ink-3">Reading the evidence record…</p>
      ) : (
        <div className="mt-3 space-y-4">
          <Scores record={record} />
          <Criteria record={record} />
          <Labels record={record} />
          <Memos record={record} />
          <Chains key={record.cell_id} record={record} />
        </div>
      )}
    </section>
  );
}

function Block({ title, note, children }: { title: string; note?: string; children: React.ReactNode }) {
  return (
    <div>
      <h3 className="text-[12px] text-ink-2">{title}</h3>
      {note ? (
        // the tool's own note: kept in full, folded away, because a rail is for scanning
        <details className="mt-0.5 mb-1.5">
          <summary className="cursor-pointer text-[11px] text-ink-3 hover:text-ink-2">note</summary>
          <p className="mt-1 text-[11.5px] text-ink-3" data-source-text>
            {note}
          </p>
        </details>
      ) : (
        <div className="mb-1.5" />
      )}
      {children}
    </div>
  );
}

// ---------- the three scores ----------

function Scores({ record }: { record: CellEvidence }) {
  const part = record.parts.cell_scores;
  if (!part) return null;
  return (
    <Block title="Scores" note={part.note}>
      <table className="w-full text-[12.5px]">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="py-1 text-left font-normal">Model</th>
            <th className="w-[80px] py-1 text-right font-normal">Score</th>
            <th className="w-[116px] py-1 pr-4 pl-3 text-right font-normal">
              <Tip
                text="Share of the labelled neighbourhood already known before scoring. A high score beside a high known share restates the record."
                className="border-line-strong border-b border-dotted"
              >
                Known share
              </Tip>
            </th>
            <th className="w-[150px] py-1 text-left font-normal">Applicability</th>
          </tr>
        </thead>
        <tbody>
          {part.rows.map((row) => {
            const model = str(row, "model");
            const applicable = row.in_area_of_applicability === true;
            return (
              <tr key={model} className="border-line border-t align-top" data-testid="score-row">
                <td className="py-1.5 text-ink-2">{model}</td>
                <td className="py-1.5 text-right">
                  <V id={idAt(row, "score_id")} />
                </td>
                <td className="py-1.5 pr-4 text-right text-ink-2">
                  <V
                    id={numberAt(row, "known_share") === null ? null : knownShareId(record.cell_id, model)}
                    emptyText="not reported"
                  />
                </td>
                <td className="py-1.5 text-[11.5px] text-ink-3">
                  {str(row, "model_note") ? (
                    <Tip text={str(row, "model_note")} className="border-line-strong border-b border-dotted">
                      {applicable ? "inside" : "outside"}
                    </Tip>
                  ) : applicable ? (
                    "inside"
                  ) : (
                    "outside"
                  )}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
    </Block>
  );
}

// ---------- the criteria breakdown ----------

const STATE_LOOK: Record<string, { label: string; tone: string }> = {
  met: { label: "met", tone: "text-st-pass" },
  "not met": { label: "not met", tone: "text-ink-3" },
  unknown: { label: "unknown", tone: "text-st-flag" },
};

function Criteria({ record }: { record: CellEvidence }) {
  const part = record.parts.criteria_breakdown;
  if (!part) return null;
  return (
    <Block title="Criteria" note={part.note}>
      <StateKey />
      <ul className="mt-2 space-y-1.5">
        {part.rows.map((row) => (
          <CriterionRow key={str(row, "criterion")} row={row} cellId={record.cell_id} />
        ))}
      </ul>
    </Block>
  );
}

function StateKey() {
  return (
    <div className="flex flex-wrap items-center gap-x-4 gap-y-1 text-[11px] text-ink-3">
      <span className="flex items-center gap-1.5">
        <span className="block h-2 w-8 rounded-full bg-st-pass" /> met
      </span>
      <span className="flex items-center gap-1.5">
        <span className="block h-2 w-8 rounded-full bg-white/[0.08]">
          <span className="block h-2 w-2 rounded-full bg-ink-3" />
        </span>
        not met
      </span>
      <span className="flex items-center gap-1.5">
        <span className="block h-2 w-8 rounded-full border border-st-flag/70 border-dashed" /> unknown (not
        measured)
      </span>
    </div>
  );
}

function CriterionRow({ row, cellId }: { row: Row; cellId: string }) {
  const key = str(row, "criterion");
  const state = str(row, "state") || "unknown";
  const unknown = state === "unknown";
  const folklore = str(row, "status") === "folklore";
  const membership = numberAt(row, "membership");
  const look = STATE_LOOK[state] ?? STATE_LOOK.unknown;
  return (
    <li
      className={cn(
        "rounded-lg border border-line border-l-2 bg-black/20 px-3 py-2",
        unknown ? "border-l-st-flag/70" : state === "met" ? "border-l-st-pass/60" : "border-l-line-strong",
        folklore && "opacity-70",
      )}
      data-testid="criterion-row"
      data-state={state}
      data-folklore={folklore ? "" : undefined}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="text-[12.5px] text-ink" data-source-text>
          {str(row, "title")}
        </span>
        <span className="font-mono text-[10.5px] text-ink-3" data-ident>
          {key}
        </span>
        <Chip>{str(row, "element")}</Chip>
        <StatusChip status={str(row, "status")} />
        <span className={cn("ml-auto text-[11.5px]", look?.tone)}>{look?.label}</span>
      </div>

      <div className="mt-1.5 flex items-center gap-3">
        <MembershipBar unknown={unknown} membership={membership} folklore={folklore} />
        <span className="w-[86px] text-right text-[11.5px]">
          {unknown ? (
            <Tip
              text={str(row, "note") || "No value here: unknown, not absent."}
              className="border-st-flag/50 border-b border-dotted"
            >
              <V id={null} emptyText="not measured" />
            </Tip>
          ) : (
            <V id={idAt(row, "membership_id")} className="text-ink-2" />
          )}
        </span>
        <span className="w-[112px] text-right text-[11px] text-ink-3">
          weight <V id={weightId(cellId, key)} />
        </span>
      </div>

      {folklore ? (
        <p className="mt-1 flex items-center gap-1.5 text-[11.5px] text-ink-3">
          <CircleSlash className="size-3.5 shrink-0" aria-hidden="true" />
          Folklore: weight zero.
        </p>
      ) : null}
      {str(row, "evidence") || str(row, "caveat") ? (
        <details className="mt-1">
          <summary className="cursor-pointer text-[11px] text-ink-3 hover:text-ink-2">source</summary>
          <div className="mt-1 space-y-1 border-line border-l pl-3">
            {str(row, "evidence") ? (
              <p className="text-[11.5px] text-ink-2" data-source-text>
                {str(row, "evidence")}
              </p>
            ) : null}
            {str(row, "caveat") ? (
              <p className="text-[11.5px] text-ink-3" data-source-text>
                {str(row, "caveat")}
              </p>
            ) : null}
          </div>
        </details>
      ) : null}
    </li>
  );
}

function StatusChip({ status }: { status: string }) {
  if (status === "folklore")
    return (
      <Chip tone="flag" className="uppercase tracking-wider">
        folklore · weight zero
      </Chip>
    );
  if (status === "published") return <Chip tone="pass">published</Chip>;
  return <Chip>{status || "unstated"}</Chip>;
}

/** Met and not met share a track and differ by fill; unknown has no track to fill. */
function MembershipBar({
  unknown,
  membership,
  folklore,
}: {
  unknown: boolean;
  membership: number | null;
  folklore: boolean;
}) {
  if (unknown) {
    return (
      <span
        className="flex h-2.5 flex-1 items-center rounded-full border border-st-flag/60 border-dashed"
        aria-hidden="true"
      />
    );
  }
  const pct = Math.max(0, Math.min(1, membership ?? 0)) * 100;
  return (
    <span className="block h-2.5 flex-1 overflow-hidden rounded-full bg-white/[0.06]" aria-hidden="true">
      <span
        className={cn("block h-full rounded-full", folklore ? "bg-ink-3/50" : "bg-st-pass/80")}
        style={{ width: `${pct}%` }}
      />
    </span>
  );
}

// ---------- the leakage check ----------

function Labels({ record }: { record: CellEvidence }) {
  const part = record.parts.label_context;
  if (!part) return null;
  return (
    <Block title="Nearest known deposit or occurrence" note={part.note}>
      <table className="w-full text-[12.5px]" data-testid="label-table">
        <tbody>
          {part.rows.map((row) => (
            <tr
              key={`${str(row, "tier")}-${str(row, "name")}-${str(row, "distance_km_id")}`}
              className="border-line border-t"
            >
              <td className="py-1.5 pr-2">
                <span className="inline-flex items-center gap-1.5">
                  <MapPin
                    className={cn(
                      "size-3.5",
                      str(row, "tier") === "deposit" ? "text-st-pass" : "text-st-flag",
                    )}
                    aria-hidden="true"
                  />
                  <span className="text-ink-2" data-source-text>
                    {str(row, "name")}
                  </span>
                </span>
              </td>
              <td className="py-1.5 pr-2 text-[11.5px] text-ink-3">{str(row, "tier")}</td>
              <td className="py-1.5 text-right">
                <V id={idAt(row, "distance_km_id")} />
              </td>
            </tr>
          ))}
        </tbody>
      </table>
    </Block>
  );
}

// ---------- the memos ----------

const ROLE_LABEL: Record<string, string> = {
  proponent: "Proponent",
  skeptic: "Skeptic",
  adjudicator: "Adjudicator",
};

function Memos({ record }: { record: CellEvidence }) {
  if (!record.memos.length) {
    return (
      <Block title="Stored memos">
        <p className="text-[12px] text-ink-3">None for this cell.</p>
      </Block>
    );
  }
  return (
    <Block
      title="Stored memos"
      note="Proponent, skeptic, adjudicator. A memo that fails the number check is kept, marked rejected."
    >
      <div className="space-y-2">
        {record.memos.map((memo) => (
          <article
            key={memo.memo_id}
            className={cn(
              "rounded-xl border border-line bg-black/20 p-3",
              !memo.published && "border-st-miss/40 bg-st-miss/[0.04]",
            )}
            data-testid="memo"
            data-published={memo.published ? "" : undefined}
            data-rejected={memo.published ? undefined : ""}
          >
            <header className="flex flex-wrap items-center gap-2">
              <h4 className="text-[12.5px] text-ink">{ROLE_LABEL[memo.role] ?? memo.role}</h4>
              {memo.published ? (
                <span className="text-[12px] text-ink-2" data-source-text>
                  {memo.verdict ?? "no verdict"}
                </span>
              ) : (
                <span className="text-[12px] text-ink-3 line-through" data-source-text>
                  {memo.verdict ?? "no verdict"}
                </span>
              )}
              {memo.published ? null : (
                <Chip tone="miss" className="uppercase tracking-wider">
                  <CircleDashed className="size-3" aria-hidden="true" /> rejected
                </Chip>
              )}
              <span className="ml-auto text-[10.5px] text-ink-3" data-chrome>
                {memo.created_at.slice(0, 16).replace("T", " ")}
              </span>
            </header>
            {memo.published ? (
              <ol className="mt-1.5 space-y-1.5">
                {memo.claims.map((claim) => (
                  <li key={`${memo.memo_id}-${claim.claim_no}`} className="text-[11.5px] text-ink-2">
                    <span data-source-text>{claim.text}</span>
                    <span className="mt-0.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
                      {claim.value_ids.some(hasValue) ? <span className="text-[10.5px]">cites</span> : null}
                      {claim.value_ids.filter(hasValue).map((id) => (
                        <V key={id} id={id} className="text-[11px] text-ink-3" />
                      ))}
                    </span>
                  </li>
                ))}
              </ol>
            ) : (
              <p className="mt-1.5 text-[11.5px] text-ink-3">Failed the number check; claims withheld.</p>
            )}
          </article>
        ))}
      </div>
    </Block>
  );
}

// ---------- the analyst chain ----------

/**
 * The three levels as words. A verdict is never coloured: colour on this rail says whether something was
 * published or withheld and whether a criterion was met, not how a cell looks.
 */
const VERDICT_WORDING: Record<ChainVerdict, string> = {
  evidence_against: "evidence against",
  insufficient: "insufficient evidence",
  supports_closer_look: "supports a closer look",
};

/** The same tones the criteria breakdown uses for the same three states. */
const NODE_STATUS_LOOK: Record<ChainNode["status"], { label: string; tone: string; edge: string }> = {
  met: { label: "met", tone: "text-st-pass", edge: "border-l-st-pass/60" },
  not_met: { label: "not met", tone: "text-ink-3", edge: "border-l-line-strong" },
  unknown: { label: "unknown", tone: "text-st-flag", edge: "border-l-st-flag/70" },
};

const NODE_GROUPS: { kind: ChainNode["kind"]; title: string }[] = [
  { kind: "criterion", title: "Criteria" },
  { kind: "crosscheck", title: "Cross-checks" },
  { kind: "retrieval", title: "Retrieval" },
];

/**
 * The ids the service registers a chain's own numbers under (`serve.chain_value_id`): the same template on
 * both sides, as for a criterion's weight, so the chain's JSON carries the numbers as stored and the panel
 * still prints each one through a value id.
 */
function chainValueId(chainId: string, ...parts: string[]): string {
  return ["c:chain", chainId, ...parts].join(":");
}

/** A chain number the service did not register (a null column) reads as absent, never as an unbacked id. */
function ChainNumber({ id, emptyText = "none" }: { id: string; emptyText?: string }) {
  return <V id={hasValue(id) ? id : null} emptyText={emptyText} />;
}

/**
 * Which decider the published verdict came from. The loop's rule is fixed (the decision stage): the
 * adjudicator's label when it ran, the majority over rounds when K was exhausted, an abstention publishing
 * "insufficient" when nothing validated, and the weighted sum alone only when no adjudicator ran. The record
 * does not name the source, so this reads the rule back off which fields are set; it computes nothing.
 */
function deciderOf(chain: AnalystChain): string {
  if (chain.abstained_reason !== null) return "abstained";
  if (chain.majority_label !== null) return "majority over rounds";
  if (chain.decision !== null) return "adjudicator";
  return "weighted sum";
}

function stamp(iso: string): string {
  return iso.slice(0, 16).replace("T", " ");
}

function Chains({ record }: { record: CellEvidence }) {
  const [picked, setPicked] = useState<string | null>(null);
  const [newest] = record.chains;
  if (!newest) {
    return (
      <Block title="Analyst chain">
        <p className="text-[12px] text-ink-3" data-testid="chain-empty">
          None for this cell yet.
        </p>
      </Block>
    );
  }
  // the newest chain unless one was picked; a pick the refetched record no longer holds falls back to it
  const chain = record.chains.find((c) => c.chain_id === picked) ?? newest;
  return (
    <Block
      title="Analyst chain"
      note="One node per criterion, checked by a verifier. A node or decision that fails its gate is withheld."
    >
      {record.chains.length > 1 ? (
        <label className="mb-2 flex items-center gap-2 text-[11px] text-ink-3">
          chain
          <select
            value={chain.chain_id}
            onChange={(e) => setPicked(e.target.value)}
            className="rounded border border-line bg-black/30 px-1.5 py-0.5 text-[11.5px] text-ink-2"
            data-testid="chain-picker"
            data-chrome
          >
            {record.chains.map((c) => (
              <option key={c.chain_id} value={c.chain_id}>
                {c.arm} · {stamp(c.created_at)}
              </option>
            ))}
          </select>
        </label>
      ) : null}
      <article
        className={cn(
          "rounded-xl border border-line bg-black/20 p-3",
          !chain.published && "border-st-miss/40 bg-st-miss/[0.04]",
        )}
        data-testid="chain"
        data-chain-id={chain.chain_id}
        data-published={chain.published ? "" : undefined}
        data-withheld={chain.published ? undefined : ""}
      >
        <ChainHeader chain={chain} />
        <ChainNodes chain={chain} />
        <ChainRounds chain={chain} />
        <Decision chain={chain} />
      </article>
    </Block>
  );
}

function WithheldChip() {
  return (
    <Chip tone="miss" className="uppercase tracking-wider">
      <CircleDashed className="size-3" aria-hidden="true" /> withheld
    </Chip>
  );
}

function ChainHeader({ chain }: { chain: AnalystChain }) {
  return (
    <header className="space-y-1">
      <div className="flex flex-wrap items-center gap-2">
        <Chip ident>{chain.arm}</Chip>
        <span className="text-[11.5px] text-ink-3">
          {chain.planner === "model" ? "model planner" : "template plan"}
        </span>
        <span className="text-[11.5px] text-ink-3">
          rounds <ChainNumber id={chainValueId(chain.chain_id, "rounds")} />
        </span>
        <span className={cn("text-[11.5px]", chain.valid ? "text-st-pass" : "text-st-miss")}>
          {chain.valid ? "validated" : "never validated"}
        </span>
        {chain.published ? <Chip tone="pass">published</Chip> : <WithheldChip />}
        <span className="ml-auto text-[10.5px] text-ink-3" data-chrome>
          {stamp(chain.created_at)}
        </span>
      </div>
      <p className="text-[12.5px] text-ink" data-testid="chain-verdict" data-verdict={chain.final_verdict}>
        {VERDICT_WORDING[chain.final_verdict]}
        <span className="text-ink-2">
          {" "}
          · probability <ChainNumber id={chainValueId(chain.chain_id, "final_probability")} /> ·{" "}
          {deciderOf(chain)}
        </span>
        {chain.cost_usd !== null ? (
          <span className="text-[11px] text-ink-3">
            {" "}
            · cost <ChainNumber id={chainValueId(chain.chain_id, "cost_usd")} />
          </span>
        ) : null}
      </p>
      {chain.abstained_reason !== null ? (
        <p className="flex items-center gap-1.5 text-[11.5px] text-st-flag" data-testid="chain-abstained">
          <ShieldQuestion className="size-3.5 shrink-0" aria-hidden="true" />
          <span>
            abstained: <span data-source-text>{chain.abstained_reason}</span>
          </span>
        </p>
      ) : null}
    </header>
  );
}

function ChainNodes({ chain }: { chain: AnalystChain }) {
  return (
    <div className="mt-2 space-y-2">
      {NODE_GROUPS.map(({ kind, title }) => {
        const nodes = chain.nodes.filter((n) => n.kind === kind);
        if (!nodes.length) return null;
        return (
          <div key={kind}>
            <h5 className="text-[10.5px] text-ink-3 uppercase tracking-wider">{title}</h5>
            <ul className="mt-1 space-y-1.5">
              {nodes.map((node) => (
                <NodeRow key={node.node_id} chainId={chain.chain_id} node={node} />
              ))}
            </ul>
          </div>
        );
      })}
    </div>
  );
}

/** The cited ids beside the text, exactly as a memo claim shows its own. */
function Cites({ ids }: { ids: string[] }) {
  const backed = ids.filter(hasValue);
  if (!backed.length) return null;
  return (
    <span className="mt-0.5 flex flex-wrap items-baseline gap-x-2 gap-y-0.5">
      <span className="text-[10.5px]">cites</span>
      {backed.map((id) => (
        <V key={id} id={id} className="text-[11px] text-ink-3" />
      ))}
    </span>
  );
}

function NodeRow({ chainId, node }: { chainId: string; node: ChainNode }) {
  const look = NODE_STATUS_LOOK[node.status];
  return (
    <li
      className={cn(
        "rounded-lg border border-line border-l-2 bg-black/20 px-3 py-2",
        look.edge,
        !node.published && "border-st-miss/40 bg-st-miss/[0.04]",
      )}
      data-testid="chain-node"
      data-node-id={node.node_id}
      data-status={node.status}
      data-withheld={node.published ? undefined : ""}
    >
      <div className="flex flex-wrap items-center gap-x-2 gap-y-1">
        <span className="font-mono text-[10.5px] text-ink-3" data-ident>
          {node.node_id}
        </span>
        <span className="text-[12.5px] text-ink" data-source-text>
          {node.criterion ?? node.segment_id}
        </span>
        {node.expert_ids.length ? (
          // B19: an expert-tier value is labelled as such wherever it is cited
          <span title={node.expert_ids.join(", ")} data-testid="chain-expert">
            <Chip tone="flag">expert-tier evidence</Chip>
          </span>
        ) : null}
        {node.published ? null : <WithheldChip />}
        <span className={cn("ml-auto text-[11.5px]", look.tone)}>{look.label}</span>
        {/* the ceiling is the protocol's constant, not a measurement, so it is chrome beside the stat */}
        <span className="text-[11px] text-ink-3" data-chrome>
          strength <ChainNumber id={chainValueId(chainId, node.node_id, "strength")} /> of 5
        </span>
      </div>
      <p className={cn("mt-1 text-[11.5px]", node.published ? "text-ink-2" : "text-ink-3 line-through")}>
        <span data-source-text>{node.text}</span>
      </p>
      <Cites ids={node.value_ids} />
      {node.depends_on.length ? (
        <p className="mt-0.5 text-[10.5px] text-ink-3">
          builds on{" "}
          <span className="font-mono" data-ident>
            {node.depends_on.join(", ")}
          </span>
        </p>
      ) : null}
      {node.published ? null : (
        <p className="mt-1 flex items-center gap-1.5 text-[11.5px] text-st-miss">
          <CircleDashed className="size-3.5 shrink-0" aria-hidden="true" />
          <span data-source-text>{node.problems[0] ?? "did not pass the node gate"}</span>
        </p>
      )}
    </li>
  );
}

function ChainRounds({ chain }: { chain: AnalystChain }) {
  if (!chain.verdicts.length) return null;
  return (
    <div className="mt-2">
      <h5 className="text-[10.5px] text-ink-3 uppercase tracking-wider">Verifier</h5>
      <ol className="mt-1 space-y-1">
        {chain.verdicts.map((v) => (
          <li
            key={v.round}
            className="text-[11.5px]"
            data-testid="chain-round"
            data-valid={v.valid ? "" : undefined}
          >
            <span className="text-ink-3" data-chrome>
              round {v.round}
            </span>{" "}
            <span className={v.valid ? "text-st-pass" : "text-st-miss"}>{v.valid ? "valid" : "invalid"}</span>
            {v.candidate_label !== null ? (
              <span className="text-ink-3">
                {" "}
                · its own label, recorded and not acted on: {VERDICT_WORDING[v.candidate_label]}
              </span>
            ) : null}
            {v.faulty.length ? (
              <ul className="mt-0.5 ml-3 space-y-0.5">
                {v.faulty.map((f) => (
                  <li key={f.node_id} className="text-ink-2">
                    <span className="font-mono text-[10.5px] text-ink-3" data-ident>
                      {f.node_id}
                    </span>{" "}
                    <span data-source-text>{f.reason}</span>
                  </li>
                ))}
              </ul>
            ) : null}
            {v.feedback ? (
              <p className="mt-0.5 text-ink-2" data-source-text>
                {v.feedback}
              </p>
            ) : null}
          </li>
        ))}
      </ol>
    </div>
  );
}

function CriteriaList({ title, items }: { title: string; items: string[] }) {
  return (
    <div>
      <div className="text-[10.5px] text-ink-3 uppercase tracking-wider">{title}</div>
      {items.length ? (
        <ul className="mt-0.5 flex flex-wrap gap-1">
          {items.map((c) => (
            <li key={c}>
              <Chip>{c}</Chip>
            </li>
          ))}
        </ul>
      ) : (
        <p className="mt-0.5 text-ink-3 italic">none</p>
      )}
    </div>
  );
}

function Decision({ chain }: { chain: AnalystChain }) {
  const d = chain.decision;
  return (
    <div className="mt-2" data-testid="chain-decision">
      <h5 className="text-[10.5px] text-ink-3 uppercase tracking-wider">Decision</h5>
      <div className="mt-1 grid gap-2 sm:grid-cols-2">
        <div className="rounded-lg border border-line bg-black/20 px-3 py-2">
          <div className="text-[10.5px] text-ink-3 uppercase tracking-wider">Weighted sum</div>
          {chain.weighted_score !== null ? (
            <p className="mt-0.5 text-[12.5px] text-ink">
              <ChainNumber id={chainValueId(chain.chain_id, "weighted_score")} />{" "}
              <span className="text-[10.5px] text-ink-3">
                weights{" "}
                <span className="font-mono" data-ident>
                  {chain.weights_version ?? "unstated"}
                </span>
              </span>
            </p>
          ) : (
            <p className="mt-0.5 text-[11.5px] text-ink-3 italic">not run</p>
          )}
        </div>
        <div
          className={cn(
            "rounded-lg border border-line bg-black/20 px-3 py-2",
            d && !d.published && "border-st-miss/40 bg-st-miss/[0.04]",
          )}
          data-withheld={d && !d.published ? "" : undefined}
        >
          <div className="flex flex-wrap items-center gap-2">
            <span className="text-[10.5px] text-ink-3 uppercase tracking-wider">Adjudicator</span>
            {d && !d.published ? <WithheldChip /> : null}
          </div>
          {d ? (
            <p className="mt-0.5 text-[12.5px] text-ink">
              {VERDICT_WORDING[d.verdict]}
              <span className="text-ink-2">
                {" "}
                · probability <ChainNumber id={chainValueId(chain.chain_id, "decision_probability")} />
              </span>
            </p>
          ) : (
            <p className="mt-0.5 text-[11.5px] text-ink-3 italic">not run</p>
          )}
          {d && !d.published ? (
            <p className="mt-1 flex items-center gap-1.5 text-[11.5px] text-st-miss">
              <CircleDashed className="size-3.5 shrink-0" aria-hidden="true" />
              <span data-source-text>{d.problems[0] ?? "did not pass the decision gate"}</span>
            </p>
          ) : null}
        </div>
      </div>
      {d ? (
        <>
          {d.published ? (
            <ol className="mt-2 space-y-1.5">
              {d.claims.map((claim) => (
                <li key={claim.text} className="text-[11.5px] text-ink-2">
                  <span data-source-text>{claim.text}</span>
                  <Cites ids={claim.value_ids} />
                </li>
              ))}
            </ol>
          ) : (
            <p className="mt-1.5 text-[11.5px] text-ink-3">Failed the number check; claims withheld.</p>
          )}
          <div className="mt-2 grid gap-2 text-[11.5px] sm:grid-cols-2">
            <CriteriaList title="Unknown: never measured here" items={d.unknown_criteria} />
            <CriteriaList title="Absent: measured here and not found" items={d.absent_criteria} />
          </div>
          <p className="mt-2 text-[11.5px] text-ink-2" data-testid="chain-observation">
            <span className="text-ink-3">Would change it: </span>
            <span data-source-text>{d.next_observation || "none named"}</span>
          </p>
          {d.rationale ? (
            <details className="mt-1">
              <summary className="cursor-pointer text-[11px] text-ink-3 hover:text-ink-2">
                the adjudicator's rationale
              </summary>
              <p className="mt-1 border-line border-l pl-3 text-[11.5px] text-ink-3" data-source-text>
                {d.rationale}
              </p>
            </details>
          ) : null}
        </>
      ) : null}
      {chain.majority_label !== null ? (
        <p className="mt-2 text-[11.5px] text-ink-2">
          Majority over rounds: {VERDICT_WORDING[chain.majority_label]}
        </p>
      ) : null}
    </div>
  );
}
