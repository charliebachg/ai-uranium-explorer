import { createContext, useContext } from "react";
import type { Manifest } from "@/data/contract";

export const ManifestContext = createContext<Manifest | null>(null);

export function useManifest(): Manifest | null {
  return useContext(ManifestContext);
}
