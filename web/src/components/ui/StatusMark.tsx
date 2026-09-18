import { cn } from "@/lib/cn";

/** Extraction status mark. Shape carries status as well as colour (readable in greyscale and for colour-blind viewers). */
export function StatusMark({
  status,
  size = 12,
  className,
}: {
  status: "pass" | "flag" | "miss" | "corrected";
  size?: number;
  className?: string;
}) {
  const s = size;
  const r = s / 2 - 1.5;
  return (
    <svg
      width={s}
      height={s}
      viewBox={`0 0 ${s} ${s}`}
      className={cn("shrink-0", className)}
      role="img"
      aria-label={status}
    >
      <title>{status}</title>
      {status === "pass" || status === "corrected" ? (
        <circle cx={s / 2} cy={s / 2} r={r} fill="#e6f0ff" />
      ) : status === "flag" ? (
        <>
          <circle cx={s / 2} cy={s / 2} r={r} fill="none" stroke="#fab219" strokeWidth="1.6" />
          <circle cx={s / 2} cy={s / 2} r={r * 0.35} fill="#fab219" />
        </>
      ) : (
        <>
          <circle cx={s / 2} cy={s / 2} r={r} fill="none" stroke="#ff5fa2" strokeWidth="1.6" />
          <path
            d={`M${s * 0.36} ${s * 0.36}L${s * 0.64} ${s * 0.64}M${s * 0.64} ${s * 0.36}L${s * 0.36} ${s * 0.64}`}
            stroke="#ff5fa2"
            strokeWidth="1.4"
          />
        </>
      )}
      {status === "corrected" ? (
        <path
          d={`M${s * 0.3} ${s * 0.52}l${s * 0.14} ${s * 0.14}l${s * 0.28} -${s * 0.3}`}
          stroke="#0b0f14"
          strokeWidth="1.4"
          fill="none"
        />
      ) : null}
    </svg>
  );
}

export function Chip({
  children,
  tone = "neutral",
  className,
  ident,
}: {
  ident?: boolean;
  children: React.ReactNode;
  tone?: "neutral" | "flag" | "miss" | "pass" | "geods" | "compilation";
  className?: string;
}) {
  const tones: Record<string, string> = {
    neutral: "bg-white/[0.06] text-ink-2",
    flag: "bg-st-flag/15 text-st-flag",
    miss: "bg-st-miss/15 text-st-miss",
    pass: "bg-st-pass/10 text-st-pass",
    geods: "bg-src-geods/15 text-src-geods",
    compilation: "bg-src-compilation/15 text-src-compilation",
  };
  return (
    <span
      data-ident={ident ? "" : undefined}
      className={cn(
        "inline-flex items-center gap-1 rounded-full px-2 py-0.5 text-[11px] leading-4",
        tones[tone],
        className,
      )}
    >
      {children}
    </span>
  );
}
