import * as Dialog from "@radix-ui/react-dialog";
import { ExternalLink, X } from "lucide-react";
import { OFM_ATTRIBUTION, TERRARIUM_ATTRIBUTION } from "@/map/style/inkStyle";
import { useStore } from "@/state/store";
import { useManifest } from "./ManifestContext";

export function AttributionDialog() {
  const open = useStore((s) => s.ui.attribution);
  const setUi = useStore((s) => s.setUi);
  const manifest = useManifest();

  return (
    <Dialog.Root open={open} onOpenChange={(o) => setUi({ attribution: o })}>
      <Dialog.Portal>
        <Dialog.Overlay className="fixed inset-0 z-40 bg-black/50 backdrop-blur-[2px]" />
        <Dialog.Content className="glass fixed top-1/2 left-1/2 z-50 max-h-[80vh] w-[680px] max-w-[92vw] -translate-x-1/2 -translate-y-1/2 overflow-y-auto rounded-2xl p-6 shadow-2xl shadow-black/60">
          <div className="flex items-start justify-between">
            <div>
              <Dialog.Title className="font-semibold text-[17px] text-ink">
                Data sources and licences
              </Dialog.Title>
              <Dialog.Description className="mt-1 text-[12.5px] text-ink-3">
                Every layer on the map, who publishes it, and whether this demo may redistribute it.
              </Dialog.Description>
            </div>
            <Dialog.Close
              className="rounded-lg p-1.5 text-ink-3 hover:bg-white/5 hover:text-ink"
              aria-label="Close"
            >
              <X className="size-4" />
            </Dialog.Close>
          </div>

          <ul className="mt-5 space-y-2.5">
            {manifest?.sources.map((s) => (
              <li key={s.id} className="rounded-xl border border-line bg-black/20 p-3.5">
                <div className="flex items-start justify-between gap-3">
                  <div className="min-w-0">
                    <div className="text-[13.5px] text-ink">{s.title}</div>
                    <div className="mt-0.5 text-[12px] text-ink-3">{s.publisher}</div>
                  </div>
                  <span
                    className={
                      s.redistributable
                        ? "shrink-0 rounded-full bg-src-geods/15 px-2 py-0.5 text-[11px] text-src-geods"
                        : "shrink-0 rounded-full bg-st-flag/15 px-2 py-0.5 text-[11px] text-st-flag"
                    }
                  >
                    {s.redistributable ? "Redistributable" : "Not redistributed"}
                  </span>
                </div>
                <div className="mt-2 text-[12px] text-ink-2">
                  {s.licence_url ? (
                    <a
                      href={s.licence_url}
                      target="_blank"
                      rel="noreferrer"
                      className="inline-flex items-center gap-1 hover:text-ink"
                    >
                      {s.licence} <ExternalLink className="size-3" />
                    </a>
                  ) : (
                    s.licence
                  )}
                </div>
                <div className="mt-1 truncate text-[11px] text-ink-3" title={s.url}>
                  {s.url} · retrieved {s.retrieved_at.slice(0, 10)}
                </div>
              </li>
            ))}
            <li className="rounded-xl border border-line bg-black/20 p-3.5 text-[12px] text-ink-2">
              <div className="mb-1 text-[13.5px] text-ink">Basemap and relief</div>
              <div
                className="text-ink-3 [&_a]:text-ink-2 [&_a:hover]:text-ink"
                // attribution strings are static config, not user input
                // biome-ignore lint/security/noDangerouslySetInnerHtml: static attribution HTML from config
                dangerouslySetInnerHTML={{ __html: `${OFM_ATTRIBUTION}<br/>${TERRARIUM_ATTRIBUTION}` }}
              />
            </li>
            <li className="rounded-xl border border-st-flag/25 bg-st-flag/[0.06] p-3.5 text-[12px] text-ink-2">
              Assessment report PDFs carry no named licence (the province publishes them "as is"). This demo
              reads them locally and never redistributes them; public builds show quotes and page references
              only.
            </li>
          </ul>
        </Dialog.Content>
      </Dialog.Portal>
    </Dialog.Root>
  );
}
