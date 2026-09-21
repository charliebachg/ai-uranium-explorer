import { readdirSync, readFileSync, statSync } from "node:fs";
import { join, resolve } from "node:path";
import { describe, expect, it } from "vitest";
import { FORBIDDEN_PHRASES,  } from "@/config/wording";

const SRC = resolve(__dirname, "../../src");

function files(dir: string): string[] {
  return readdirSync(dir).flatMap((f) => {
    const p = join(dir, f);
    return statSync(p).isDirectory() ? files(p) : /\.(tsx?|css)$/.test(f) ? [p] : [];
  });
}

describe("wording", () => {

  it("contains no forbidden phrases anywhere in the app source", () => {
    for (const f of files(SRC)) {
      if (f.endsWith("wording.ts")) continue;
      const text = readFileSync(f, "utf8").toLowerCase();
      for (const phrase of FORBIDDEN_PHRASES) expect(text.includes(phrase), `${phrase} in ${f}`).toBe(false);
    }
  });

  it("only formats numbers in lib/format.ts (and instrument-free spikes)", () => {
    const allowed = /(lib\/format\.ts|spikes\/)/;
    for (const f of files(SRC)) {
      if (allowed.test(f)) continue;
      const text = readFileSync(f, "utf8");
      expect(/\.toFixed\(|toLocaleString\(|Intl\.NumberFormat/.test(text), `number formatting in ${f}`).toBe(
        false,
      );
    }
  });
});
