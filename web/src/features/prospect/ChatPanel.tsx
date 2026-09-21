import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, SendHorizontal, Wrench } from "lucide-react";
import { useEffect, useRef, useState, useSyncExternalStore } from "react";
import { Chip } from "@/components/ui/StatusMark";
import { V } from "@/components/values/V";
import type {
  ChainDiff,
  ChatAbstention,
  ChatInsight,
  ChatJob,
  ChatRoute,
  InterfaceTurn,
  RecordedChat,
  RecordedJob,
  RecordedTurn,
  StoredTurn,
} from "@/data/contract";
import { loadRecordedChat } from "@/data/loader";
import { hasValue, registryVersion, subscribeRegistry } from "@/data/registry";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";
import { AnswerText, type CiteHandler } from "./AnswerText";
import { conversationCostId, jobCostId, registerChatCost, registerJobCost, turnCostId } from "./cellValues";
import { OfflineNotice, type ServiceState } from "./OfflineNotice";
import { jobFinished, keys, useConversation, useConversations, useJob } from "./queries";
import { askStreaming, type ChatEvent } from "./service";

/**
 * A conversation about the selected cell, laid out as one: the question on the right, the answer on the left,
 * the composer pinned at the bottom.
 *
 * The one thing that reads differently from an ordinary chat is what happens when an answer fails the check.
 * It is not retried quietly and it is not dropped. The turn stays in the transcript with its text withheld and
 * the checker's objection in its place, because "the agent said something it could not back" is the most
 * informative thing this panel ever has to show.
 *
 * Since Phase 4c every turn is routed first (PRD §8.3), and the transcript says so: the route line names the
 * kind the question was read as and the plan that fetched its evidence; a refusal shows its reason rather than
 * an apology; an insight shows the expert-tier id it was recorded under; an invoked analyst is a card that
 * polls its job, and the turn that finds it finished carries the verdict beside the stored chain without the
 * insight.
 */

const SUGGESTIONS = [
  "What is actually measured here, and what is only assumed?",
  "How much of this score is explained by where people have already drilled?",
  "Which criteria are unknown here rather than not met?",
];

/**
 * A persisted turn, in the shape the panel draws: what the gate published, its claims, and its objections.
 * The route, the abstention and the insight are not persisted as such; the actions are, as tool calls named
 * `abstain`, `record_insight` and `run_analyst`, so a resumed transcript still says what the agent did.
 */
function fromStored(t: StoredTurn): InterfaceTurn {
  return {
    question: t.question,
    text: t.text,
    claims: t.claims,
    caveats: [],
    cannot_answer: false,
    published: t.published,
    problems: t.problems,
    tools_used: t.tool_calls.map((c) => c.tool),
    cost_usd: t.cost_usd ?? undefined,
    jobs_done: [],
    expert_ids: [],
    retried: false,
  };
}

/** A turn to draw: live from the service, resumed from the store, or replayed from the recording. */
type Entry = { turn: InterfaceTurn | RecordedTurn; conversationId: string; index: number };

export function ChatPanel({
  cellId,
  state,
  onCite,
}: {
  cellId: string | null;
  state: ServiceState;
  onCite?: CiteHandler;
}) {
  const replay = useStore((s) => s.chatReplay);
  const [recorded, setRecorded] = useState<RecordedChat | null>(null);
  useEffect(() => {
    if (replay && !recorded) loadRecordedChat().then(setRecorded, () => undefined);
  }, [replay, recorded]);
  // the walkthrough lengthens the replay step by step; the newest turn shown is the one being talked about
  const replayFoot = useRef<HTMLDivElement | null>(null);
  const replayTurns = replay?.turns ?? 0;
  // biome-ignore lint/correctness/useExhaustiveDependencies: the turn count is the trigger, not an input
  useEffect(() => {
    replayFoot.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [replayTurns, recorded]);

  const [entries, setEntries] = useState<Entry[]>([]);
  const [conversationId, setConversationId] = useState<string | null>(null);
  const [question, setQuestion] = useState("");
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [forCell, setForCell] = useState<string | null>(null);
  const [live, setLive] = useState<ChatEvent[]>([]);
  const foot = useRef<HTMLDivElement | null>(null);
  // earlier conversations the service persisted about this cell, and the one being resumed
  const [resumeId, setResumeId] = useState<string | null>(null);
  const history = useConversations(cellId, state === "up");
  const stored = useConversation(resumeId);
  const queryClient = useQueryClient();

  // a conversation is about one cell: selecting another starts a new one rather than carrying the old context
  if (cellId !== forCell) {
    setForCell(cellId);
    setEntries([]);
    setConversationId(null);
    setResumeId(null);
    setError(null);
  }
  const storedRecord = stored.data;
  useEffect(() => {
    if (!storedRecord || storedRecord.conversation_id !== resumeId || storedRecord.cell_id !== cellId) return;
    setEntries(
      storedRecord.turns.map((t, index) => ({
        turn: fromStored(t),
        conversationId: storedRecord.conversation_id,
        index,
      })),
    );
    setConversationId(storedRecord.conversation_id);
  }, [storedRecord, resumeId, cellId]);

  const turns = entries.length;
  // biome-ignore lint/correctness/useExhaustiveDependencies: turns and busy are the triggers, not inputs
  useEffect(() => {
    foot.current?.scrollIntoView({ block: "end", behavior: "smooth" });
  }, [turns, busy]);

  const send = (text: string) => {
    if (!cellId || busy || !text.trim()) return;
    setBusy(true);
    setError(null);
    setQuestion("");
    setLive([]);
    askStreaming(
      {
        cell_id: cellId,
        question: text.trim(),
        ...(conversationId ? { conversation_id: conversationId } : {}),
      },
      (event) =>
        setLive((prev) => {
          // a delta is one fragment of the reply being written; it grows the line rather than adding one
          const last = prev[prev.length - 1];
          if (event.type === "delta" && last?.type === "delta" && last.step === event.step) {
            return [...prev.slice(0, -1), { ...last, text: last.text + event.text }];
          }
          return [...prev, event];
        }),
    )
      .then((answer) => {
        const index = entries.length;
        registerChatCost(answer.conversation_id, index, answer.turn.cost_usd, answer.cost_usd);
        setConversationId(answer.conversation_id);
        setEntries((prev) => [...prev, { turn: answer.turn, conversationId: answer.conversation_id, index }]);
        void queryClient.invalidateQueries({ queryKey: keys.conversations(cellId) });
      })
      .catch((e: unknown) => setError(String(e)))
      .finally(() => {
        setBusy(false);
        setLive([]);
      });
  };

  const last = entries[entries.length - 1];

  // The walkthrough shows a session that already happened, rather than keeping a room waiting on a model
  // call. It is labelled as recorded: a replayed answer is evidence of what the agent said once, not a promise
  // about what it would say now. The turns are the agent's own record, drawn exactly as a live one is: the
  // route line, a refusal with its reason, the job card as its row ended, and the diff the follow-up reported.
  if (replay && recorded && replay.cellId === cellId) {
    const shown = recorded.turns.slice(0, Math.max(0, replay.turns));
    return (
      <div className="flex h-full min-h-0 flex-col" data-testid="chat-panel" data-replay="">
        <div className="flex shrink-0 flex-wrap items-baseline gap-x-2 gap-y-1 px-3 pt-2 pb-1.5">
          <Chip tone="neutral" className="uppercase tracking-wider">
            recorded
          </Chip>
          <span className="text-[11px] text-ink-3">
            a real session, replayed: every number was checked against the tool values, and the analyst job
            ran to the end when it was recorded
          </span>
          <span className="ml-auto text-[11px] text-ink-3" data-chrome>
            {recorded.recorded_at.slice(0, 10)} · {recorded.model}
          </span>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 pb-3" data-testid="chat-log">
          {shown.map((turn, i) => (
            <Exchange
              key={turn.question}
              entry={{ turn, conversationId: `recorded-${recorded.cell_id}`, index: i }}
              onCite={onCite}
              cellId={cellId}
              replay
            />
          ))}
          <div ref={replayFoot} />
        </div>
      </div>
    );
  }

  if (state !== "up") {
    return (
      <div className="px-3 py-2" data-testid="chat-panel">
        <p className="mb-2 text-[11.5px] text-ink-3">
          Answers are checked against the tool values before they are shown.
        </p>
        <OfflineNotice state={state} what="The conversation needs the local model process." />
      </div>
    );
  }

  if (!cellId) {
    return (
      <div className="px-3 py-3" data-testid="chat-panel">
        <p className="text-[12.5px] text-ink-3">Select a cell first.</p>
      </div>
    );
  }

  return (
    <div className="flex h-full min-h-0 flex-col" data-testid="chat-panel">
      <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 pt-2.5 pb-2" data-testid="chat-log">
        {entries.length === 0 ? (
          <div>
            <p className="text-[12px] text-ink-3">
              Every number is checked against the tool values before an answer is shown; one that fails is
              withheld, with the objection in its place.
            </p>
            <div className="mt-3 flex flex-col items-start gap-1.5">
              {SUGGESTIONS.map((s) => (
                <button
                  key={s}
                  type="button"
                  disabled={busy}
                  onClick={() => send(s)}
                  className="rounded-2xl rounded-bl-sm border border-line bg-white/[0.03] px-3 py-1.5 text-left text-[11.5px] text-ink-3 transition-colors hover:border-focus/40 hover:text-ink-2 disabled:opacity-50"
                >
                  {s}
                </button>
              ))}
            </div>
            {history.data && history.data.conversations.length > 0 ? (
              <div className="mt-4" data-testid="chat-history">
                <p className="text-[11px] text-ink-3 uppercase tracking-wider">
                  Earlier conversations about this cell
                </p>
                <ul className="mt-1.5 space-y-1">
                  {history.data.conversations.map((c) => (
                    <li key={c.conversation_id}>
                      <button
                        type="button"
                        onClick={() => setResumeId(c.conversation_id)}
                        className="w-full rounded-md border border-line px-2.5 py-1.5 text-left text-[11.5px] text-ink-2 transition-colors hover:text-ink"
                        data-testid="chat-history-item"
                      >
                        <span data-chrome>{c.created_at.slice(0, 16).replace("T", " ")}</span>
                        <span className="text-ink-3">
                          {" "}
                          · {c.turns === 1 ? "1 turn" : `${c.turns} turns`} · {c.model}
                        </span>
                      </button>
                    </li>
                  ))}
                </ul>
              </div>
            ) : null}
          </div>
        ) : null}

        {entries.map((entry) => (
          <Exchange
            key={`${entry.conversationId}-${entry.index}`}
            entry={entry}
            onCite={onCite}
            cellId={cellId}
          />
        ))}

        {busy ? <Working events={live} /> : null}
        {error ? <p className="text-[12px] text-st-miss">{error}</p> : null}
        <div ref={foot} />
      </div>

      <form
        className="shrink-0 border-line border-t px-3 py-2"
        onSubmit={(e) => {
          e.preventDefault();
          send(question);
        }}
      >
        <div className="flex items-center gap-2">
          <input
            value={question}
            onChange={(e) => setQuestion(e.target.value)}
            placeholder="Ask about this cell"
            aria-label="Ask about this cell"
            data-testid="chat-input"
            className="min-w-0 flex-1 rounded-full border border-line bg-black/30 px-3.5 py-2 text-[12.5px] text-ink outline-none placeholder:text-ink-3 focus:border-focus/60"
          />
          <button
            type="submit"
            disabled={busy || !question.trim()}
            data-testid="chat-send"
            aria-label="Ask"
            className="flex size-9 shrink-0 items-center justify-center rounded-full bg-raised text-ink-2 transition-colors hover:text-ink disabled:opacity-40"
          >
            <SendHorizontal className="size-4" aria-hidden="true" />
          </button>
        </div>
        {last && hasValue(conversationCostId(last.conversationId)) ? (
          <p className="mt-1.5 text-right text-[10.5px] text-ink-3">
            conversation so far <V id={conversationCostId(last.conversationId)} />
          </p>
        ) : null}
      </form>
    </div>
  );
}

const TOOL_SAYS: Record<string, string> = {
  cell_features: "reading the cell's features",
  cell_scores: "reading the three scores",
  criteria_breakdown: "reading the criteria breakdown",
  label_context: "checking the nearest known deposit",
  coverage: "checking how much of the grid that feature covers",
  retrieve: "searching the assessment corpus",
  nearby: "counting one evidence layer around the cell",
  crosscheck: "computing the conductor-fault and sediment-sampling pairs",
  sensitivity: "ranking the unmeasured criteria by how far each would move the score",
  abstain: "recording a refusal with its reason",
  record_insight: "writing the statement to the expert tier",
  run_analyst: "handing the cell to the analyst",
};

/** The route line's words for each kind: what the agent read the question as. */
const KIND_SAYS: Record<ChatRoute["kind"], string> = {
  lookup: "a lookup",
  compare: "a comparison of two cells",
  explain_score: "explain the score",
  what_is_unknown: "what is unknown here",
  what_would_change: "what would change the reading",
  record_insight: "record an insight",
  run_analyst: "run the analyst",
  other: "unrouted: the plain tool loop",
};

const REASON_SAYS: Record<ChatAbstention["reason"], string> = {
  not_measured: "not measured here",
  outside_grid: "outside the grid",
  no_value: "no value in the store",
  out_of_scope: "out of scope",
};

const VERDICT_SAYS: Record<string, string> = {
  evidence_against: "evidence against",
  insufficient: "insufficient evidence",
  supports_closer_look: "supports a closer look",
};

function verdictWords(v: string | null | undefined): string {
  return v ? (VERDICT_SAYS[v] ?? v) : "no verdict";
}

function RouteLine({ route }: { route: ChatRoute }) {
  return (
    <p
      className="flex flex-wrap items-baseline gap-x-1.5 gap-y-1 text-[10.5px] text-ink-3"
      data-testid="chat-route"
    >
      <span className="uppercase tracking-wider">routed as</span>
      <span className="text-ink-2" data-route-kind={route.kind}>
        {KIND_SAYS[route.kind]}
      </span>
      {route.out_of_scope ? <Chip tone="flag">out of scope</Chip> : null}
      {route.plan.length ? (
        <span className="font-mono" data-ident>
          {route.plan.map((s) => s.tool).join(" · ")}
        </span>
      ) : null}
      {route.fallback ? <span className="italic">{route.fallback}</span> : null}
    </p>
  );
}

/**
 * What the agent is doing, while it does it — including *why*, which is the part worth watching.
 *
 * Each step of the loop returns its own reasoning before it returns an action, so the panel can show the
 * agent deciding it needs the label context before it goes and gets it. The last line always carries the
 * blinking dots, because a list that stops updating and a list that has finished look identical otherwise.
 */
function Dots() {
  return (
    <span className="ue-dots inline-flex gap-[3px]" aria-hidden="true">
      <span className="size-1 rounded-full bg-ink-3" />
      <span className="size-1 rounded-full bg-ink-3" />
      <span className="size-1 rounded-full bg-ink-3" />
    </span>
  );
}

function Working({ events }: { events: ChatEvent[] }) {
  // a model call can run for the better part of a minute; a clock is the cheapest way to say it is alive
  const [since] = useState(() => Date.now());
  const [now, setNow] = useState(() => Date.now());
  useEffect(() => {
    const t = window.setInterval(() => setNow(Date.now()), 1000);
    return () => window.clearInterval(t);
  }, []);
  const seconds = Math.max(0, Math.round((now - since) / 1000));

  return (
    <div
      className="space-y-1.5 rounded-xl border border-line border-dashed px-3 py-2"
      data-testid="chat-thinking"
      aria-live="polite"
      aria-busy="true"
    >
      <p className="flex items-baseline justify-between text-[10.5px] text-ink-3">
        <span className="uppercase tracking-wider">working</span>
        <span className="tabular" data-instrument="elapsed">
          {seconds}s
        </span>
      </p>
      {events.map((e, i) => {
        const last = i === events.length - 1;
        return (
          // biome-ignore lint/suspicious/noArrayIndexKey: a step log is ordered, not keyed
          <div key={i} className="text-[11.5px] text-ink-3">
            {e.type === "opened" ? (
              <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="text-ink-2">staged the cell's evidence</span>
                <span className="font-mono text-[10.5px]" data-ident>
                  {e.tools.join(" · ")}
                </span>
              </p>
            ) : e.type === "thinking" ? (
              <p className="flex items-center gap-2">
                <span className="text-ink-2">thinking</span>
                {last ? <Dots /> : null}
              </p>
            ) : e.type === "delta" ? (
              <p className="border-line border-l pl-2.5 font-mono text-[10.5px] break-words" data-source-text>
                {e.text.slice(-400)}
                {last ? <Dots /> : null}
              </p>
            ) : e.type === "reasoning" ? (
              <p className="border-line border-l pl-2.5 italic" data-source-text>
                {e.text}
              </p>
            ) : e.type === "tool" ? (
              <p className="flex flex-wrap items-baseline gap-x-2 gap-y-1">
                <span className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-[10.5px]" data-ident>
                  {e.tool}
                </span>
                <span>{TOOL_SAYS[e.tool] ?? "calling a tool"}</span>
                {last ? <Dots /> : null}
              </p>
            ) : e.type === "tool_error" ? (
              <p className="text-st-flag">
                {e.tool} could not answer: {e.error}
              </p>
            ) : e.type === "checking" ? (
              <p className="flex items-center gap-2">
                <span className="text-ink-2">checking every number against the tool values</span>
                {last ? <Dots /> : null}
              </p>
            ) : e.type === "refused" ? (
              <p className="text-st-flag">
                the check objected; asking once more with{" "}
                {e.problems.length === 1 ? "the objection" : "the objections"}
                {last ? <Dots /> : null}
              </p>
            ) : e.type === "route" ? (
              <RouteLine route={e} />
            ) : e.type === "abstain" ? (
              <p className="text-st-flag">declining: {REASON_SAYS[e.reason]}</p>
            ) : e.type === "insight" ? (
              <p>
                recorded in the expert tier as{" "}
                <span className="font-mono text-[10.5px]" data-ident>
                  {e.expert_id}
                </span>
              </p>
            ) : e.type === "job" ? (
              <p>
                analyst job{" "}
                <span className="font-mono text-[10.5px]" data-ident>
                  {e.job_id}
                </span>{" "}
                {e.status}
              </p>
            ) : (
              <p className="text-st-miss">{e.error}</p>
            )}
          </div>
        );
      })}
      {events.length === 0 ? (
        <p className="flex items-center gap-2 text-[11.5px] text-ink-3">
          <span>reading the tools</span>
          <Dots />
        </p>
      ) : null}
    </div>
  );
}

/** A refusal, with its reason where an answer would be: the reason is the answer. */
function Abstention({ abstention }: { abstention: ChatAbstention }) {
  return (
    <div data-testid="chat-abstention" data-reason={abstention.reason}>
      <Chip tone="flag" className="uppercase tracking-wider">
        declined
      </Chip>
      <p className="mt-1.5 text-[12px] text-ink">
        <span className="text-ink-2">{REASON_SAYS[abstention.reason]}</span>
        {abstention.said ? <span className="text-ink-3"> · {abstention.said}</span> : null}
      </p>
      {abstention.detail ? (
        <p className="mt-1 text-[11.5px] text-ink-3" data-source-text>
          {abstention.detail}
        </p>
      ) : null}
      <p className="mt-1 font-mono text-[10.5px] text-ink-3" data-ident>
        {abstention.abstain_id}
      </p>
    </div>
  );
}

/** A statement written to the expert tier: its id, its author, and the expert-tier values minted from it. */
function Insight({ insight }: { insight: ChatInsight }) {
  return (
    <div className="mt-2 rounded-lg border border-line border-dashed px-2.5 py-2" data-testid="chat-insight">
      <p className="flex flex-wrap items-baseline gap-x-2 text-[10.5px] text-ink-3">
        <span className="uppercase tracking-wider">expert tier</span>
        <span className="font-mono" data-ident>
          {insight.expert_id}
        </span>
        <span>by {insight.author}</span>
      </p>
      <p className="mt-1 text-[11.5px] text-ink-2 italic" data-source-text>
        {insight.text}
      </p>
      {insight.value_ids.some(hasValue) ? (
        <p className="mt-1 flex flex-wrap items-baseline gap-1.5">
          {insight.value_ids.filter(hasValue).map((id) => (
            <V key={id} id={id} className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10.5px] text-ink-2" />
          ))}
        </p>
      ) : (
        <p className="mt-1 text-[10.5px] text-ink-3">no numbers in it; nothing minted</p>
      )}
    </div>
  );
}

/** The session assessment: the verdict with the insight beside the verdict without it, and the nodes that moved. */
function Assessment({ diff }: { diff: ChainDiff }) {
  const moved = diff.nodes.filter((n) => n.changed);
  return (
    <div className="mt-1.5 space-y-1 text-[11px]" data-testid="chat-assessment">
      <p className="text-ink-2">
        verdict {diff.verdict.changed ? "changed" : "unchanged"}:{" "}
        <span className="text-ink-3">{verdictWords(diff.verdict.before)}</span>
        <span className="text-ink-3"> → </span>
        <span>{verdictWords(diff.verdict.after)}</span>
      </p>
      {moved.length ? (
        <ul className="space-y-0.5 border-line border-l pl-2.5 text-ink-3">
          {moved.map((n) => (
            <li key={n.node_id}>
              <span className="font-mono text-[10.5px]" data-ident>
                {n.criterion ?? n.node_id}
              </span>{" "}
              {n.before ?? "absent"} → {n.after ?? "absent"}
              {n.expert_ids.length ? <span className="text-st-flag"> · leans on your insight</span> : null}
            </li>
          ))}
        </ul>
      ) : (
        <p className="text-ink-3">no node changed its status</p>
      )}
      <p className="text-ink-3">{diff.note}</p>
    </div>
  );
}

/** The stages a job's row reports, as words: `stage:verify` reads as "verify". Numbers on the events stay there. */
function stagesOf(progress: RecordedJob["progress"] | undefined): string[] {
  return (progress ?? [])
    .map((e) => e.event)
    .filter((name) => name.startsWith("stage:"))
    .map((name) => name.slice("stage:".length));
}

/**
 * An analyst job the conversation invoked, polled until it finishes. Done, its verdict and its cost are shown
 * (the cost as a value, registered from the row) and the evidence query is invalidated so the chains section
 * lists the new chain; the diff against the stored chain arrives with the next turn, which reports it. In a
 * replay the row is the recording's, already final, and nothing is polled.
 */
function JobCard({
  job,
  cellId,
  replay = false,
}: {
  job: ChatJob | RecordedJob;
  cellId: string | null;
  replay?: boolean;
}) {
  const queryClient = useQueryClient();
  // the cost is registered once the row says done; the card re-renders when the registry gains it
  useSyncExternalStore(subscribeRegistry, registryVersion);
  const live = useJob(job.job_id, !replay && !jobFinished(job.status));
  const status = live.data?.status ?? job.status;
  const result = live.data?.result ?? job.result ?? null;
  const verdict = (result?.verdict as string | undefined) ?? job.verdict ?? null;
  const error = live.data?.error ?? job.error ?? null;
  // the recorded row carries its progress on the job itself, where a live one is read off the poll
  const stages = stagesOf(live.data?.progress ?? (job as Partial<RecordedJob>).progress);
  const finished = jobFinished(status);
  useEffect(() => {
    if (!finished) return;
    registerJobCost(job.job_id, result?.cost_usd);
    if (cellId && !replay) void queryClient.invalidateQueries({ queryKey: keys.evidence(cellId) });
  }, [finished, job.job_id, result, cellId, queryClient, replay]);
  return (
    <div
      className="mt-2 rounded-lg border border-line px-2.5 py-2"
      data-testid="chat-job"
      data-status={status}
    >
      <p className="flex flex-wrap items-baseline gap-x-2 text-[10.5px] text-ink-3">
        <span className="uppercase tracking-wider">analyst job</span>
        <span className="font-mono" data-ident>
          {job.job_id}
        </span>
        <Chip
          tone={
            status === "done" ? "pass" : status === "failed" || status === "cancelled" ? "miss" : "neutral"
          }
        >
          {status}
        </Chip>
        {!finished ? <Dots /> : null}
      </p>
      <p className="mt-1 text-[11px] text-ink-3">
        handed over: the cell, {job.score_ids.length ? "its out-of-fold score ids" : "no out-of-fold score"},{" "}
        {job.expert_ids.length
          ? `${job.expert_ids.length === 1 ? "the insight" : "the insights"} recorded here`
          : "no insight"}
        {job.reason ? <span className="italic"> · "{job.reason}"</span> : null}
      </p>
      {stages.length ? (
        <p
          className="mt-1 flex flex-wrap items-baseline gap-x-1.5 text-[10.5px] text-ink-3"
          data-testid="chat-job-stages"
        >
          <span className="uppercase tracking-wider">stages</span>
          <span className="font-mono" data-ident>
            {stages.join(" · ")}
          </span>
        </p>
      ) : null}
      {finished ? (
        <p className="mt-1 text-[11.5px] text-ink">
          {status === "done" ? (
            <>
              verdict: {verdictWords(verdict)}
              {hasValue(jobCostId(job.job_id)) ? (
                <span className="text-ink-3">
                  {" "}
                  · cost <V id={jobCostId(job.job_id)} />
                </span>
              ) : null}
            </>
          ) : (
            <span className="text-st-miss">{error ?? status}</span>
          )}
        </p>
      ) : null}
      {job.assessment ? <Assessment diff={job.assessment} /> : null}
    </div>
  );
}

/** One question and its answer, as a pair of messages. */
function Exchange({
  entry,
  onCite,
  cellId = null,
  replay = false,
}: {
  entry: Entry;
  onCite?: CiteHandler;
  cellId?: string | null;
  /** a recorded turn: its job card is drawn from the recording and never polls the service */
  replay?: boolean;
}) {
  const { turn } = entry;
  const costId = turnCostId(entry.conversationId, entry.index);
  const declined = turn.abstention ?? null;
  return (
    <div
      className="space-y-2"
      data-testid="chat-turn"
      data-published={turn.published ? "" : undefined}
      data-withheld={turn.published ? undefined : ""}
      data-kind={turn.route?.kind}
    >
      <div className="flex justify-end">
        <p
          className="max-w-[85%] rounded-2xl rounded-br-sm bg-raised px-3 py-2 text-[12.5px] text-ink"
          data-source-text
        >
          {turn.question}
        </p>
      </div>

      <div className="flex justify-start">
        <div
          className={cn(
            "max-w-[93%] rounded-2xl rounded-bl-sm border px-3 py-2",
            turn.published ? "border-line bg-white/[0.04]" : "border-st-miss/40 bg-st-miss/[0.05]",
            declined && "border-st-flag/40 bg-st-flag/[0.04]",
          )}
        >
          {turn.route ? <RouteLine route={turn.route} /> : null}
          {turn.jobs_done.map((job) => (
            <div key={job.job_id} className="mb-2" data-testid="chat-job-done">
              <p className="text-[10.5px] text-ink-3 uppercase tracking-wider">the analyst finished</p>
              <JobCard job={job} cellId={cellId} replay={replay} />
            </div>
          ))}
          {declined ? (
            <Abstention abstention={declined} />
          ) : turn.published ? (
            <>
              <AnswerText text={turn.text ?? ""} onCite={onCite} />
              {turn.claims.length ? (
                <ul className="mt-2 space-y-1.5 border-line border-l pl-2.5">
                  {turn.claims.map((claim) => (
                    <li key={claim.text} className="text-[11px] text-ink-3">
                      <span data-source-text>{claim.text}</span>
                      {claim.value_ids.some(hasValue) ? (
                        <span className="mt-0.5 flex flex-wrap items-baseline gap-1.5">
                          {claim.value_ids.filter(hasValue).map((id) => (
                            <V
                              key={id}
                              id={id}
                              className="rounded bg-white/[0.06] px-1.5 py-0.5 text-[10.5px] text-ink-2"
                            />
                          ))}
                        </span>
                      ) : null}
                    </li>
                  ))}
                </ul>
              ) : null}
              {turn.caveats.length ? (
                <ul className="mt-2 space-y-1">
                  {turn.caveats.map((c) => (
                    <li key={c} className="flex gap-1.5 text-[11px] text-ink-3">
                      <AlertTriangle className="mt-[2px] size-3 shrink-0" aria-hidden="true" />
                      <span data-source-text>{c}</span>
                    </li>
                  ))}
                </ul>
              ) : null}
              {turn.insight ? <Insight insight={turn.insight} /> : null}
              {turn.job ? <JobCard job={turn.job} cellId={cellId} replay={replay} /> : null}
            </>
          ) : (
            <>
              <Chip tone="miss" className="uppercase tracking-wider">
                withheld
              </Chip>
              <p className="mt-1.5 text-[11.5px] text-ink-3">The check objected to:</p>
              <ul className="mt-1 space-y-1">
                {turn.problems.map((p) => (
                  <li key={p} className="flex gap-1.5 text-[11px] text-st-miss">
                    <AlertTriangle className="mt-[2px] size-3 shrink-0" aria-hidden="true" />
                    <span data-source-text>{p}</span>
                  </li>
                ))}
              </ul>
            </>
          )}

          <div className="mt-2 flex flex-wrap items-center gap-x-2.5 gap-y-1 text-[10.5px] text-ink-3">
            {turn.tools_used.length ? (
              <span className="flex flex-wrap items-center gap-1">
                <Wrench className="size-2.5" aria-hidden="true" />
                {[...new Set(turn.tools_used)].map((tool) => (
                  <span key={tool} className="rounded bg-white/[0.06] px-1 py-0.5 font-mono" data-ident>
                    {tool}
                  </span>
                ))}
              </span>
            ) : (
              <span>answered from what was already read</span>
            )}
            {turn.cannot_answer && !declined ? (
              <span className="text-st-flag">the agent declined</span>
            ) : null}
            {turn.retried ? <span className="text-st-flag">answered on the second ask</span> : null}
            {turn.expert_ids.length ? (
              <span className="text-st-flag">leans on your insight (expert tier)</span>
            ) : null}
            {hasValue(costId) ? (
              <span className="ml-auto">
                <V id={costId} />
              </span>
            ) : null}
          </div>
        </div>
      </div>
    </div>
  );
}
