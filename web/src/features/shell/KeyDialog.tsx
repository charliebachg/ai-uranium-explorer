import * as Dialog from "@radix-ui/react-dialog";
import { KeyRound, X } from "lucide-react";
import { useId, useState, useSyncExternalStore } from "react";
import { api, readKey, subscribeKey, writeKey } from "@/api/client";
import { cn } from "@/lib/cn";

/**
 * The API key, when the service has a key register (PRD §A.2: roles viewer, geologist, admin over one register
 * shared with the MCP server). Kept in this browser's localStorage and sent as `X-Api-Key` on every call the
 * typed client makes. With no register the service answers a local page as `local` with every role, so most
 * of the time this dialog is not needed and says so; a key is checked against `/api/whoami` before it is
 * kept, and the roles it holds are shown in words.
 */

function keyState(): string {
  return readKey() ?? "";
}

/** The button for the top bar: says whether a key is set, opens the dialog. */
export function KeyButton() {
  const [open, setOpen] = useState(false);
  const key = useSyncExternalStore(subscribeKey, keyState);
  return (
    <>
      <button
        type="button"
        onClick={() => setOpen(true)}
        className={cn(
          "flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 text-[12px] transition-colors",
          key ? "bg-raised text-ink" : "bg-black/25 text-ink-3 hover:text-ink-2",
        )}
        aria-label={key ? "API key set; change it" : "Set an API key"}
        aria-pressed={!!key}
        data-testid="key-button"
      >
        <KeyRound className="size-3.5" aria-hidden="true" />
        <span className="hidden lg:inline">{key ? "Key set" : "Key"}</span>
      </button>
      <KeyDialog open={open} onOpenChange={setOpen} />
    </>
  );
}

type Check =
  | { state: "idle" }
  | { state: "checking" }
  | { state: "ok"; name: string; roles: string[] }
  | { state: "bad"; why: string };

export function KeyDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (open: boolean) => void }) {
  const stored = useSyncExternalStore(subscribeKey, keyState);
  const [draft, setDraft] = useState<string | null>(null);
  const [check, setCheck] = useState<Check>({ state: "idle" });
  const inputId = useId();
  const value = draft ?? stored;

  const verify = async () => {
    setCheck({ state: "checking" });
    // the document declares no error body for this route, so the 401's `detail` is read untyped
    const out = (await api
      .GET("/api/whoami", { headers: value ? { "X-Api-Key": value } : {} })
      .catch(() => null)) as {
      data?: { name: string; roles: string[] };
      error?: unknown;
    } | null;
    if (out?.data) setCheck({ state: "ok", name: out.data.name, roles: out.data.roles });
    else {
      const detail =
        out?.error && typeof out.error === "object" && "detail" in out.error
          ? String((out.error as { detail: unknown }).detail)
          : "the service did not answer";
      setCheck({ state: "bad", why: detail });
    }
  };
  const save = () => {
    writeKey(value || null);
    setDraft(null);
    onOpenChange(false);
  };

  return (
    <Dialog.Root
      open={open}
      onOpenChange={(o) => {
        if (!o) {
          setDraft(null);
          setCheck({ state: "idle" });
        }
        onOpenChange(o);
      }}
    >
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50 backdrop-blur-[2px]" />
        <Dialog.Content className="glass fixed top-1/2 left-1/2 z-50 w-[460px] max-w-[92vw] -translate-x-1/2 -translate-y-1/2 rounded-2xl p-6 shadow-2xl shadow-black/60">
          <div className="flex items-start justify-between">
            <div>
              <Dialog.Title className="font-semibold text-[17px] text-ink">API key</Dialog.Title>
              <Dialog.Description className="mt-1 text-[12.5px] text-ink-3">
                Sent with every request as <code data-chrome>X-Api-Key</code>. A local service with no key
                register needs none; one with a register refuses the chat and the jobs without it.
              </Dialog.Description>
            </div>
            <Dialog.Close
              className="rounded-lg p-1.5 text-ink-3 hover:bg-white/5 hover:text-ink"
              aria-label="Close"
            >
              <X className="size-4" />
            </Dialog.Close>
          </div>
          <label htmlFor={inputId} className="mt-4 block text-[11px] text-ink-3 uppercase tracking-[0.12em]">
            Key
          </label>
          <input
            id={inputId}
            type="password"
            autoComplete="off"
            spellCheck={false}
            value={value}
            onChange={(e) => {
              setDraft(e.target.value);
              setCheck({ state: "idle" });
            }}
            className="mt-1 w-full rounded-lg border border-line bg-black/25 px-2.5 py-1.5 font-mono text-[12.5px] text-ink outline-none focus:border-focus/70"
            placeholder="leave empty for a local service"
            data-testid="key-input"
          />
          <p className="mt-2 min-h-[1.25rem] text-[12px] text-ink-2" data-testid="key-check">
            {check.state === "checking" ? "checking…" : null}
            {check.state === "ok"
              ? `accepted as ${check.name}: ${check.roles.length ? check.roles.join(", ") : "no role"}`
              : null}
            {check.state === "bad" ? check.why : null}
          </p>
          <div className="mt-3 flex items-center gap-2">
            <button
              type="button"
              onClick={() => void verify()}
              className="rounded-lg bg-black/25 px-2.5 py-1.5 text-[12px] text-ink-3 hover:text-ink-2"
              data-testid="key-check-button"
            >
              Check
            </button>
            <button
              type="button"
              onClick={save}
              className="rounded-lg bg-raised px-2.5 py-1.5 text-[12px] text-ink hover:bg-white/10"
              data-testid="key-save"
            >
              {value ? "Keep" : "Clear"}
            </button>
            <span className="ml-auto text-[11px] text-ink-3">kept in this browser only</span>
          </div>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
