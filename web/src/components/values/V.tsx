import { useSyncExternalStore } from "react";
import { registryVersion, resolveValue, subscribeRegistry } from "@/data/registry";
import { cn } from "@/lib/cn";
import { formatVal, unitOf } from "@/lib/format";

/**
 * <V id> is the only way the app prints a stored number. It renders the value (as printed for extracted values),
 * marks the element with data-vid, and fails loudly for unknown ids: throws in dev, renders an UNBACKED chip in
 * a build (and counts it on window.__ue.unbacked so the browser test catches it).
 */

type Props = {
  id: string | null | undefined;
  unit?: boolean;
  className?: string;
  onSelect?: (id: string) => void;
  emptyText?: string;
};

function noteUnbacked(id: string) {
  const w = window as unknown as { __ue?: { unbacked?: string[] } };
  w.__ue = w.__ue ?? {};
  w.__ue.unbacked = [...(w.__ue.unbacked ?? []), id];
}

export function V({ id, unit = true, className, onSelect, emptyText = "not printed" }: Props) {
  useSyncExternalStore(subscribeRegistry, registryVersion);
  if (!id) {
    return <span className={cn("text-ink-3 italic", className)}>{emptyText}</span>;
  }
  const v = resolveValue(id);
  if (!v) {
    if (import.meta.env.DEV) throw new Error(`<V> unbacked value id ${id}`);
    noteUnbacked(id);
    return (
      <span data-unbacked={id} className="rounded bg-st-miss/20 px-1 text-st-miss text-[11px]">
        UNBACKED
      </span>
    );
  }
  const text = formatVal(v);
  const u = unit ? unitOf(v) : null;
  const content = (
    <>
      {text}
      {u ? <span className="text-ink-3">{u === "°" || u === "%" ? u : ` ${u}`}</span> : null}
    </>
  );
  if (onSelect) {
    return (
      <button
        type="button"
        data-vid={v.id}
        data-kind={v.kind}
        className={cn(
          "tabular cursor-pointer whitespace-nowrap rounded-sm underline decoration-line-strong underline-offset-4 hover:decoration-ink-2",
          className,
        )}
        onClick={() => onSelect(v.id)}
        title={v.note}
      >
        {content}
      </button>
    );
  }
  return (
    <span
      data-vid={v.id}
      data-kind={v.kind}
      className={cn("tabular whitespace-nowrap", className)}
      title={v.note}
    >
      {content}
    </span>
  );
}
