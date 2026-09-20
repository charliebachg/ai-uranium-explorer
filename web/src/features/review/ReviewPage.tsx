import { Check, ImageOff, RefreshCw, X } from "lucide-react";
import { useCallback, useEffect, useMemo, useState } from "react";
import { readKey, subscribeKey } from "@/api/client";
import { Chip } from "@/components/ui/StatusMark";
import type { BBox, ReviewItem, ReviewQueuePage, ReviewReading } from "@/data/contract";
import { pageImageUrl } from "@/data/reports";
import { OfflineNotice, type ServiceState } from "@/features/prospect/OfflineNotice";
import { health } from "@/features/prospect/service";
import { usePages } from "@/features/report/useReport";
import { cn } from "@/lib/cn";
import { type Decision, listReview, ReviewError, resolveReview } from "./service";

/**
 * The review queue (PRD §8.2 stage 4): every value the two reader families disagreed on, or that only one of
 * them found, with both readings side by side and the page cropped to the box the quote was located at. A
 * geologist accepts one reading or rejects both; the decision is recorded on the item with who made it, and
 * neither reading is ever rewritten. Readings are shown as printed text with the model that read them: they
 * are evidence about a page, not numbers this page computed.
 */
export function ReviewPage() {
  const [service, setService] = useState<ServiceState>("unknown");
  const [data, setData] = useState<ReviewQueuePage | null>(null);
  const [error, setError] = useState<string | null>(null);
  const [file, setFile] = useState<string | null>(null);
  const [files, setFiles] = useState<string[]>([]);
  const [offset, setOffset] = useState(0);
  const [hasKey, setHasKey] = useState(!!readKey());
  const limit = 25;

  useEffect(() => subscribeKey(() => setHasKey(!!readKey())), []);

  const load = useCallback(async () => {
    setError(null);
    try {
      const page = await listReview({ file, limit, offset });
      setData(page);
      setService("up");
      if (!file) setFiles(Array.from(new Set(page.items.map((i) => i.file_num))).sort());
    } catch (e) {
      const up = await health();
      setService(up ? "up" : "down");
      if (up) setError(e instanceof Error ? e.message : String(e));
    }
  }, [file, offset]);

  useEffect(() => {
    load();
  }, [load]);

  const resolved = useCallback((item: ReviewItem) => {
    setData((d) =>
      d
        ? {
            ...d,
            items: d.items.filter((i) => i.queue_id !== item.queue_id),
            total: Math.max(0, d.total - 1),
          }
        : d,
    );
  }, []);

  return (
    <div className="h-full overflow-y-auto bg-ground" data-strict="review">
      <div className="mx-auto max-w-[1100px] space-y-4 px-6 py-6">
        <header className="glass rounded-2xl px-4 py-3">
          <div className="flex flex-wrap items-center gap-3">
            <h1 className="text-[15px] text-ink">Review queue</h1>
            <span className="text-[12px] text-ink-3">
              what the second reader disagreed with the first about; an accepted reading is filed with your
              key's label, and the two readings stay as they were
            </span>
            {data ? (
              <span className="ml-auto text-[12px] text-ink-3" data-chrome>
                {data.total} open{file ? ` in ${file}` : ""}
                {Object.entries(data.counts)
                  .filter(([k]) => k !== "open")
                  .map(([k, n]) => ` · ${n} ${k.replace("_", " ")}`)
                  .join("")}
              </span>
            ) : null}
          </div>
          <div className="mt-2 flex flex-wrap items-center gap-2 text-[12px]">
            <label className="flex items-center gap-1.5 text-ink-3">
              file
              <select
                className="rounded-md bg-black/25 px-2 py-1 text-ink"
                value={file ?? ""}
                onChange={(e) => {
                  setOffset(0);
                  setFile(e.target.value || null);
                }}
              >
                <option value="">every file</option>
                {files.map((f) => (
                  <option key={f} value={f}>
                    {f}
                  </option>
                ))}
              </select>
            </label>
            <button
              type="button"
              className="flex items-center gap-1 rounded-md bg-black/25 px-2 py-1 text-ink-2 hover:text-ink"
              onClick={() => load()}
            >
              <RefreshCw className="size-3" /> refresh
            </button>
            {!hasKey ? (
              <span className="text-ink-3">
                no API key set: decisions are accepted from this machine only when the service runs without a
                key register
              </span>
            ) : null}
          </div>
        </header>

        {service === "down" ? (
          <OfflineNotice state="down" what="The queue lives in the store the service reads." />
        ) : null}
        {error ? <div className="text-[13px] text-st-miss">{error}</div> : null}
        {data && data.items.length === 0 && service === "up" ? (
          <div className="text-[13px] text-ink-3">
            Nothing open{file ? ` in ${file}` : ""}. Run{" "}
            <code className="rounded bg-white/[0.06] px-1.5 py-0.5 font-mono text-ink-2">
              lr extract agent
            </code>{" "}
            over read pages to compare a second family with the first.
          </div>
        ) : null}

        {data?.items.map((item) => (
          <ItemCard key={item.queue_id} item={item} onResolved={resolved} />
        ))}

        {data && data.total > limit ? (
          <div className="flex items-center gap-2 text-[12px] text-ink-3" data-chrome>
            <button
              type="button"
              disabled={offset === 0}
              className="rounded-md bg-black/25 px-2 py-1 disabled:opacity-40"
              onClick={() => setOffset(Math.max(0, offset - limit))}
            >
              newer
            </button>
            <span>
              {offset + 1}–{Math.min(offset + limit, data.total)} of {data.total}
            </span>
            <button
              type="button"
              disabled={offset + limit >= data.total}
              className="rounded-md bg-black/25 px-2 py-1 disabled:opacity-40"
              onClick={() => setOffset(offset + limit)}
            >
              older
            </button>
          </div>
        ) : null}
      </div>
    </div>
  );
}

const REASON: Record<ReviewItem["reason"], string> = {
  disagreed: "the two readers disagree",
  only_a: "only the first reader found this",
  only_b: "only the second reader found this",
};

function ItemCard({ item, onResolved }: { item: ReviewItem; onResolved: (item: ReviewItem) => void }) {
  const [busy, setBusy] = useState<Decision | null>(null);
  const [problem, setProblem] = useState<string | null>(null);
  const box = useMemo(() => cropBox(item), [item]);

  const decide = async (decision: Decision) => {
    setBusy(decision);
    setProblem(null);
    try {
      const done = await resolveReview(item.queue_id, decision);
      onResolved(done);
    } catch (e) {
      setProblem(e instanceof ReviewError ? e.message : String(e));
      setBusy(null);
    }
  };

  return (
    <article className="glass rounded-2xl p-4" data-testid="review-item">
      <div className="flex flex-wrap items-center gap-2 text-[12px]">
        <span className="text-ink" data-ident>
          {item.file_num}
        </span>
        <span className="text-ink-3" data-chrome>
          p{item.page}
        </span>
        <Chip>{item.field}</Chip>
        {item.field_type ? <Chip tone="neutral">{item.field_type}</Chip> : null}
        <Chip tone={item.reason === "disagreed" ? "flag" : "miss"}>{REASON[item.reason]}</Chip>
        {item.value_id ? (
          <span className="ml-auto font-mono text-[11px] text-ink-3" data-ident>
            {item.value_id}
          </span>
        ) : null}
      </div>

      <div className="mt-3 grid grid-cols-[1fr_1fr_minmax(260px,1.2fr)] gap-3 max-md:grid-cols-1">
        <ReadingCard label="first reader" model={item.model_a} reading={item.reading_a} />
        <ReadingCard label="second reader" model={item.model_b} reading={item.reading_b} />
        <Crop file={item.file_num} page={item.page} box={box} />
      </div>

      <div className="mt-3 flex flex-wrap items-center gap-2">
        <DecideButton
          label="accept first"
          tone="pass"
          disabled={!item.reading_a || !!busy}
          busy={busy === "accepted_a"}
          onClick={() => decide("accepted_a")}
        />
        <DecideButton
          label="accept second"
          tone="pass"
          disabled={!item.reading_b || !!busy}
          busy={busy === "accepted_b"}
          onClick={() => decide("accepted_b")}
        />
        <DecideButton
          label="reject both"
          tone="miss"
          disabled={!!busy}
          busy={busy === "rejected"}
          onClick={() => decide("rejected")}
        />
        {problem ? <span className="text-[12px] text-st-miss">{problem}</span> : null}
      </div>
    </article>
  );
}

function DecideButton({
  label,
  tone,
  disabled,
  busy,
  onClick,
}: {
  label: string;
  tone: "pass" | "miss";
  disabled: boolean;
  busy: boolean;
  onClick: () => void;
}) {
  return (
    <button
      type="button"
      disabled={disabled}
      onClick={onClick}
      className={cn(
        "flex items-center gap-1.5 rounded-md px-2.5 py-1.5 text-[12px] transition-colors disabled:opacity-40",
        tone === "pass"
          ? "bg-st-pass/10 text-st-pass hover:bg-st-pass/20"
          : "bg-st-miss/15 text-st-miss hover:bg-st-miss/25",
      )}
    >
      {tone === "pass" ? <Check className="size-3.5" /> : <X className="size-3.5" />}
      {busy ? "recording…" : label}
    </button>
  );
}

function ReadingCard({
  label,
  model,
  reading,
}: {
  label: string;
  model: string | null | undefined;
  reading: ReviewReading | null;
}) {
  return (
    <div className="rounded-xl border border-line bg-black/20 p-3">
      <div className="flex items-center gap-2 text-[11px] text-ink-3">
        {label}
        {model ? (
          <span className="font-mono" data-ident>
            {model}
          </span>
        ) : null}
      </div>
      {reading ? (
        <>
          <div className="mt-1.5 font-mono text-[15px] text-ink" data-source-text>
            {reading.as_printed}
            {reading.unit_as_printed ? (
              <span className="ml-1 text-[12px] text-ink-3">{reading.unit_as_printed}</span>
            ) : null}
          </div>
          {reading.analyte ? (
            <div className="text-[11px] text-ink-3" data-source-text>
              {reading.analyte}
            </div>
          ) : null}
          {reading.quote && reading.quote !== reading.as_printed ? (
            <p
              className="mt-1.5 border-line border-l pl-2 text-[11px] text-ink-3 break-words"
              data-source-text
            >
              “{reading.quote}”
            </p>
          ) : null}
          <div className="mt-1.5 text-[11px] text-ink-3">
            {reading.bbox
              ? "quote located on the page"
              : reading.row_bbox
                ? "row located; the value itself was not"
                : "not located"}
          </div>
        </>
      ) : (
        <div className="mt-1.5 text-[13px] text-ink-3">not found by this reader</div>
      )}
    </div>
  );
}

/** The box to crop to: the first reading's own box, else the second's, else either reading's row band. */
function cropBox(item: ReviewItem): BBox | null {
  return (
    item.reading_a?.bbox ??
    item.reading_b?.bbox ??
    item.reading_a?.row_bbox ??
    item.reading_b?.row_bbox ??
    null
  );
}

const CROP_W = 300;
const CROP_H = 96;

/** The page image cropped around the box, with the box outlined; the image and its size come from pages.json. */
function Crop({ file, page, box }: { file: string; page: number; box: BBox | null }) {
  const { data: pages } = usePages(file);
  const meta = pages?.pages.find((p) => p.page === page) ?? null;
  const url = pageImageUrl(meta?.image ?? null);
  if (!meta || !url || !box) {
    return (
      <div className="flex items-center justify-center gap-2 rounded-xl border border-line bg-black/20 text-[11.5px] text-ink-3">
        <ImageOff className="size-4" />
        {!box ? "no box on either reading" : !meta ? "page not exported" : "no page image in this build"}
      </div>
    );
  }
  const [x0, y0, x1, y1] = box;
  const bw = Math.max((x1 - x0) * meta.width_px, 12);
  const bh = Math.max((y1 - y0) * meta.height_px, 10);
  // the box fills about a third of the crop, never enlarged past 2x or shrunk past a quarter
  const scale = Math.min(Math.max(Math.min((CROP_H * 0.42) / bh, (CROP_W * 0.6) / bw), 0.25), 2);
  const cx = ((x0 + x1) / 2) * meta.width_px * scale;
  const cy = ((y0 + y1) / 2) * meta.height_px * scale;
  const left = CROP_W / 2 - cx;
  const top = CROP_H / 2 - cy;
  return (
    <div
      className="relative overflow-hidden rounded-xl border border-line bg-white"
      style={{ width: CROP_W, height: CROP_H }}
    >
      <img
        src={url}
        alt={`page ${page} around the value`}
        draggable={false}
        style={{
          position: "absolute",
          left,
          top,
          width: meta.width_px * scale,
          height: meta.height_px * scale,
          maxWidth: "none",
        }}
      />
      <div
        className="pointer-events-none absolute rounded-sm border-2 border-st-flag"
        style={{
          left: left + x0 * meta.width_px * scale - 2,
          top: top + y0 * meta.height_px * scale - 2,
          width: bw * scale + 4,
          height: bh * scale + 4,
        }}
      />
    </div>
  );
}
