import { CircleDashed, CircleSlash, MapPin, ShieldQuestion } from "lucide-react";
import { Chip } from "@/components/ui/StatusMark";
import { V } from "@/components/values/V";
import type { CellEvidence } from "@/data/contract";
import { hasValue } from "@/data/registry";
import { cn } from "@/lib/cn";
import { knownShareId, weightId } from "./cellValues";
import { OfflineNotice, type ServiceState } from "./OfflineNotice";

/**
 * Everything the local service holds about one cell: the three scores, what each criterion contributed, how
 * close the nearest labelled deposit is, and the memos three agents wrote about it.
 *
 * Two distinctions carry the panel. A criterion that is unknown here is not a criterion that failed: it is
 * drawn with no bar at all and named as unmeasured, while a criterion that was measured and fell short gets a
 * bar and a number. And a memo that did not pass the evidence check is shown as rejected, with its verdict
 * struck out and its claims withheld, so it reads as a record of a failure rather than as an argument.
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
        <OfflineNotice
          state={state}
          what="The evidence record, the memos and the chat all come from that process."
        />
      ) : !cellId ? (
        <p className="mt-3 text-[12.5px] text-ink-3">
          Click a cell on the map, or pick one of the ranked cells above it, to see what is recorded there.
        </p>
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
          <summary className="cursor-pointer text-[11px] text-ink-3 hover:text-ink-2">
            why this matters
          </summary>
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
    <Block title="What each model says here" note={part.note}>
      <table className="w-full text-[12.5px]">
        <thead className="text-[10.5px] text-ink-3 uppercase tracking-wider">
          <tr>
            <th className="py-1 text-left font-normal">Model</th>
            <th className="w-[80px] py-1 text-right font-normal">Score</th>
            <th className="w-[104px] py-1 pr-4 text-right font-normal">Known share</th>
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
                  {applicable ? "inside the area of applicability" : "outside the area of applicability"}
                </td>
              </tr>
            );
          })}
        </tbody>
      </table>
      {part.rows.map((row) =>
        str(row, "model_note") ? (
          <p key={`${str(row, "model")}-note`} className="mt-1.5 text-[11px] text-ink-3">
            <span className="text-ink-2">{str(row, "model")}</span>:{" "}
            <span data-source-text>{str(row, "model_note")}</span>
          </p>
        ) : null,
      )}
      <p className="mt-1.5 text-[11px] text-ink-3">
        Known share is how much of this cell's labelled neighbourhood was already known before any of this was
        scored. A high score beside a high known share is a restatement of the record, not a finding.
      </p>
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
    <Block title="What each criterion contributed" note={part.note}>
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
        <span className="block h-2 w-8 rounded-full bg-st-pass" /> met: measured here and above the threshold
      </span>
      <span className="flex items-center gap-1.5">
        <span className="block h-2 w-8 rounded-full bg-white/[0.08]">
          <span className="block h-2 w-2 rounded-full bg-ink-3" />
        </span>
        not met: measured here and below it
      </span>
      <span className="flex items-center gap-1.5">
        <span className="block h-2 w-8 rounded-full border border-st-flag/70 border-dashed" /> unknown: never
        measured here, which is not a failure
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
            <V id={null} emptyText="not measured" />
          ) : (
            <V id={idAt(row, "membership_id")} className="text-ink-2" />
          )}
        </span>
        <span className="w-[112px] text-right text-[11px] text-ink-3">
          weight <V id={weightId(cellId, key)} />
        </span>
      </div>

      {unknown ? (
        <p className="mt-1 flex items-center gap-1.5 text-[11.5px] text-st-flag">
          <ShieldQuestion className="size-3.5 shrink-0" aria-hidden="true" />
          <span data-source-text>{str(row, "note") || "no value for this criterion here"}</span>
        </p>
      ) : null}
      {folklore ? (
        <p className="mt-1 flex items-center gap-1.5 text-[11.5px] text-ink-3">
          <CircleSlash className="size-3.5 shrink-0" aria-hidden="true" />
          Carried at weight zero: named in the handbook as personal communication with no published test, so
          it contributes nothing to the score whatever its membership.
        </p>
      ) : null}
      {str(row, "evidence") || str(row, "caveat") ? (
        <details className="mt-1">
          <summary className="cursor-pointer text-[11px] text-ink-3 hover:text-ink-2">
            Where this criterion comes from
          </summary>
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
        <p className="text-[12px] text-ink-3">No memo has been written for this cell.</p>
      </Block>
    );
  }
  return (
    <Block
      title="Stored memos"
      note="Three agents argue the same evidence: one for, one against, one adjudicating. A memo whose claims did not check out against the tool values is kept and shown as rejected rather than deleted."
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
              <p className="mt-1.5 text-[11.5px] text-ink-3">
                This memo failed the check that every number it cites came from a tool, so its claims are not
                shown here. It is kept as a record of what the agent tried to say, not offered as argument.
              </p>
            )}
          </article>
        ))}
      </div>
    </Block>
  );
}
