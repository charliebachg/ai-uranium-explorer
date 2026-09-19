import { useQueryClient } from "@tanstack/react-query";
import { AlertTriangle, SendHorizontal, Wrench } from "lucide-react";
import { useEffect, useRef, useState } from "react";
import { Chip } from "@/components/ui/StatusMark";
import { V } from "@/components/values/V";
import type { ChatTurn, RecordedChat, StoredTurn } from "@/data/contract";
import { loadRecordedChat } from "@/data/loader";
import { hasValue } from "@/data/registry";
import { cn } from "@/lib/cn";
import { useStore } from "@/state/store";
import { AnswerText, type CiteHandler } from "./AnswerText";
import { conversationCostId, registerChatCost, turnCostId } from "./cellValues";
import { OfflineNotice, type ServiceState } from "./OfflineNotice";
import { keys, useConversation, useConversations } from "./queries";
import { askStreaming, type ChatEvent } from "./service";

/**
 * A conversation about the selected cell, laid out as one: the question on the right, the answer on the left,
 * the composer pinned at the bottom.
 *
 * The one thing that reads differently from an ordinary chat is what happens when an answer fails the check.
 * It is not retried quietly and it is not dropped. The turn stays in the transcript with its text withheld and
 * the checker's objection in its place, because "the agent said something it could not back" is the most
 * informative thing this panel ever has to show.
 */

const SUGGESTIONS = [
  "What is actually measured here, and what is only assumed?",
  "How much of this score is explained by where people have already drilled?",
  "Which criteria are unknown here rather than not met?",
];

/** A persisted turn, in the shape the panel draws: what the gate published, its claims, and its objections. */
function fromStored(t: StoredTurn): ChatTurn {
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
  };
}

type Entry = { turn: ChatTurn; conversationId: string; index: number };

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

  // The walkthrough shows a conversation that already happened, rather than keeping a room waiting on a model
  // call. It is labelled as recorded: a replayed answer is evidence of what the agent said once, not a promise
  // about what it would say now.
  if (replay && recorded && replay.cellId === cellId) {
    const shown = recorded.turns.slice(0, Math.max(0, replay.turns));
    return (
      <div className="flex h-full min-h-0 flex-col" data-testid="chat-panel" data-replay="">
        <div className="flex shrink-0 flex-wrap items-baseline gap-x-2 gap-y-1 px-3 pt-2 pb-1.5">
          <Chip tone="neutral" className="uppercase tracking-wider">
            recorded
          </Chip>
          <span className="text-[11px] text-ink-3">
            a real exchange, replayed; every number was checked against the tool values
          </span>
          <span className="ml-auto text-[11px] text-ink-3" data-chrome>
            {recorded.recorded_at.slice(0, 10)}
          </span>
        </div>
        <div className="min-h-0 flex-1 space-y-3 overflow-y-auto px-3 pb-3" data-testid="chat-log">
          {shown.map((turn, i) => (
            <Exchange
              key={turn.question}
              entry={{ turn, conversationId: `recorded-${recorded.cell_id}`, index: i }}
              onCite={onCite}
            />
          ))}
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
          <Exchange key={`${entry.conversationId}-${entry.index}`} entry={entry} onCite={onCite} />
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
};

/**
 * What the agent is doing, while it does it — including *why*, which is the part worth watching.
 *
 * Each step of the loop returns its own reasoning before it returns an action, so the panel can show the
 * agent deciding it needs the label context before it goes and gets it. The last line always carries the
 * blinking dots, because a list that stops updating and a list that has finished look identical otherwise.
 */
function Dots() {
  return (
    <span className="lr-dots inline-flex gap-[3px]" aria-hidden="true">
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

/** One question and its answer, as a pair of messages. */
function Exchange({ entry, onCite }: { entry: Entry; onCite?: CiteHandler }) {
  const { turn } = entry;
  const costId = turnCostId(entry.conversationId, entry.index);
  return (
    <div
      className="space-y-2"
      data-testid="chat-turn"
      data-published={turn.published ? "" : undefined}
      data-withheld={turn.published ? undefined : ""}
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
          )}
        >
          {turn.published ? (
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
            {turn.cannot_answer ? <span className="text-st-flag">the agent declined</span> : null}
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
