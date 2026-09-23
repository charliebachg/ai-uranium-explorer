import { V } from "@/components/values/V";
import { hasValue, resolveValue } from "@/data/registry";

/**
 * The agent writes its answer with value ids inline, because that is what the gate checks. Printed raw it
 * reads like a stack trace: "the criteria-based score is 0.634 (c:score:0107_0034:criteria), built from a
 * known-input share of 0.8 (c:score:0107_0034:criteria:known)".
 *
 * So the ids are lifted out and rendered as what they point at — a chip carrying the stored value, which can
 * be clicked to open the thing it refers to. Nothing is hidden: the citation is still there, it is just shown
 * as the evidence rather than as its address. An id the registry does not know is dropped rather than printed,
 * because a dead reference on screen is worse than no reference.
 */

/**
 * Value ids as the tools mint them: `c:score:0107_0034:criteria`, `c:metric:effort.spatial.pr_auc`, and the
 * interface agent's `c:sens:…` and `c:insight:…`. `ID` scans a chunk (global); `WHOLE_ID` tests one token, and
 * is a separate, non-global regex on purpose: a global regex remembers where its last match ended, so testing
 * tokens with it skipped every second citation group in a paragraph, and the ids after it were never chips.
 */
const ID = /c:[a-z]+:[A-Za-z0-9_.:-]+/g;
const WHOLE_ID = /^c:[a-z]+:[A-Za-z0-9_.:-]+$/;
/** A parenthesised run of nothing but citations, which is how the agent almost always writes them. */
const CITATION_GROUP = /\(\s*(c:[A-Za-z0-9_.:,\s-]+?)\s*\)/g;
/** The cell an id belongs to, so a reader can jump to it: the second segment of most ids is the cell. */
const CELL_IN_ID = /^c:[a-z]+:(\d{4}_\d{4})\b/;

export type CiteHandler = (id: string, cellId: string | null) => void;

/** A number the agent typed just before its citation, with the unit it wrote after it. */
const TYPED_BEFORE = /(-?\d[\d,]*(?:\.\d+)?)(\s*(?:%|°|m|km|km2|km²|ppm|cps|USD|years?|metres|meters))?\s*$/;

/** Whether a typed number is the stored value a chip will print: equal at the precision it was typed. */
function sameNumber(typed: string, id: string): boolean {
  const v = resolveValue(id)?.value;
  if (typeof v !== "number") return false;
  const t = Number(typed.replace(/,/g, ""));
  const decimals = typed.split(".")[1]?.length ?? 0;
  return Number.isFinite(t) && Math.abs(t - v) <= 0.5 * 10 ** -decimals + 1e-9;
}

/**
 * The agent writes "0.9013 (id)": the number, then its citation. The chip prints the stored value, so the
 * typed number before it is dropped when it is that same value, or every figure reads twice. A number that is
 * not the cited value ("within 5 km (year id)") stays as written.
 */
function dedupe(out: Piece[]): Piece[] {
  for (let i = 1; i < out.length; i++) {
    const cur = out[i];
    const prev = out[i - 1];
    if (!cur || !prev || !("ids" in cur) || !("text" in prev)) continue;
    const m = TYPED_BEFORE.exec(prev.text);
    const typed = m?.[1];
    if (!m || !typed || !cur.ids.some((id) => hasValue(id) && sameNumber(typed, id))) continue;
    prev.text = prev.text.slice(0, m.index);
  }
  return out;
}

type Piece = { text: string } | { ids: string[] };

function pieces(text: string): Piece[] {
  // pull whole "(id, id)" groups first so their brackets go with them, then any id left standing alone
  const marked = text.replace(CITATION_GROUP, (whole, inner: string) => {
    const ids = inner.split(/[,\s]+/).filter(Boolean);
    return ids.every((v) => WHOLE_ID.test(v)) ? `\u0000${ids.join(" ")}\u0000` : whole;
  });

  const out: Piece[] = [];
  for (const [i, chunk] of marked.split("\u0000").entries()) {
    if (!chunk) continue;
    if (i % 2 === 1) {
      out.push({ ids: chunk.split(/\s+/).filter(Boolean) });
      continue;
    }
    // a bare id outside brackets still becomes a chip
    let last = 0;
    for (const m of chunk.matchAll(ID)) {
      if (m.index > last) out.push({ text: chunk.slice(last, m.index) });
      out.push({ ids: [m[0]] });
      last = m.index + m[0].length;
    }
    if (last < chunk.length) out.push({ text: chunk.slice(last) });
  }
  return dedupe(out);
}

/** Split a paragraph the agent wrote as a run-on list back into the list it meant. */
function bullets(text: string): { lead: string; items: string[] } {
  const parts = text.split(/(?:^|\s)-\s+(?=[A-Z(])/);
  const lead = parts.shift() ?? "";
  return { lead: lead.trim(), items: parts.map((p) => p.trim()).filter(Boolean) };
}

function Line({ text, onCite }: { text: string; onCite?: CiteHandler }) {
  return (
    <>
      {pieces(text).map((piece, i) =>
        "text" in piece ? (
          // biome-ignore lint/suspicious/noArrayIndexKey: prose fragments have no identity of their own
          <span key={i}>{piece.text}</span>
        ) : (
          // biome-ignore lint/suspicious/noArrayIndexKey: same
          <span key={i} className="inline-flex flex-wrap items-baseline gap-1 align-baseline">
            {piece.ids.filter(hasValue).map((id) => (
              <V
                key={id}
                id={id}
                className="rounded bg-white/[0.07] px-1.5 py-0.5 text-[11px] text-ink-2"
                {...(onCite ? { onSelect: () => onCite(id, CELL_IN_ID.exec(id)?.[1] ?? null) } : {})}
              />
            ))}
          </span>
        ),
      )}
    </>
  );
}

export function AnswerText({ text, onCite }: { text: string; onCite?: CiteHandler }) {
  const paragraphs = text.split(/\n{2,}|\n/).filter((p) => p.trim());
  return (
    <div className="space-y-2 text-[12.5px] text-ink leading-relaxed" data-source-text>
      {paragraphs.map((paragraph) => {
        const { lead, items } = bullets(paragraph);
        return (
          <div key={paragraph.slice(0, 48)}>
            {lead ? (
              <p>
                <Line text={lead} onCite={onCite} />
              </p>
            ) : null}
            {items.length ? (
              <ul className="mt-1.5 space-y-1.5">
                {items.map((item) => (
                  <li key={item.slice(0, 48)} className="flex gap-2">
                    <span className="mt-[7px] size-1 shrink-0 rounded-full bg-ink-3" aria-hidden="true" />
                    <span>
                      <Line text={item} onCite={onCite} />
                    </span>
                  </li>
                ))}
              </ul>
            ) : null}
          </div>
        );
      })}
    </div>
  );
}
