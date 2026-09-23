import { type ReactNode, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { cn } from "@/lib/cn";

const WIDTH = 320;
const GAP = 6;

/**
 * A hover and focus tooltip that shows at once. The native `title` waits a second and some browsers never show
 * it, so a help mark that relied on it looked broken. The bubble is portalled to the body with fixed
 * coordinates: a table's scroll container cannot clip it, and it sits outside every strict region, so a note
 * with numbers in it is not a digit on the page.
 */
export function Tip({
  text,
  children,
  className,
  testId,
  label,
}: {
  text: string;
  /** the mark's own name, when it has no text of its own (an icon) */
  label?: string;
  children: ReactNode;
  className?: string;
  testId?: string;
}) {
  const ref = useRef<HTMLButtonElement>(null);
  const id = useId();
  const [at, setAt] = useState<{ left: number; top: number; above: boolean } | null>(null);
  const show = () => {
    const r = ref.current?.getBoundingClientRect();
    if (!r) return;
    const half = WIDTH / 2 + 8;
    const left = Math.min(Math.max(r.left + r.width / 2, half), window.innerWidth - half);
    const above = r.bottom + 90 > window.innerHeight;
    setAt({ left, top: above ? r.top - GAP : r.bottom + GAP, above });
  };
  const hide = () => setAt(null);
  if (!text) return <>{children}</>;
  return (
    // a button, so a keyboard reaches the note and a screen reader is told it describes the mark
    <button
      ref={ref}
      type="button"
      onMouseEnter={show}
      onMouseLeave={hide}
      onFocus={show}
      onBlur={hide}
      aria-label={label}
      aria-describedby={at ? id : undefined}
      className={cn(
        "cursor-help text-inherit outline-none [letter-spacing:inherit] [text-transform:inherit] focus-visible:ring-1 focus-visible:ring-line-strong",
        className,
      )}
      data-testid={testId}
    >
      {children}
      {at
        ? createPortal(
            <span
              id={id}
              role="tooltip"
              data-testid="tip"
              style={{ left: at.left, top: at.top, maxWidth: WIDTH }}
              className={cn(
                "pointer-events-none fixed z-[1000] -translate-x-1/2 rounded-lg border border-line bg-raised px-2.5 py-1.5 text-left font-normal text-[11.5px] text-ink-2 normal-case leading-snug tracking-normal shadow-lg",
                at.above && "-translate-y-full",
              )}
            >
              {text}
            </span>,
            document.body,
          )
        : null}
    </button>
  );
}
