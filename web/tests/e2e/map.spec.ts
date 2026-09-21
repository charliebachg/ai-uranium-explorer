import { expect, test } from "@playwright/test";

test("map loads real provincial data, hover and selection work", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    // the chat service on :8787 is optional by design; the browser logs its refused connection as an error
    if (m.type() === "error" && !m.location().url.includes(":8787/")) errors.push(m.text());
  });
  await page.goto("/?intro=0"); // the opening sequence has its own spec
  await expect(page.getByTestId("honesty-banner")).toHaveText(
    "Hole locations from public reports and provincial compilations. Colour shows extraction status, not prospectivity.",
  );
  await expect(page.getByTestId("coverage")).toContainText("5,822");

  await page.waitForFunction(
    () => {
      const m = (
        window as unknown as {
          __ue?: { map?: { isSourceLoaded: (s: string) => boolean; loaded: () => boolean } };
        }
      ).__ue?.map;
      try {
        return !!m && m.isSourceLoaded("compilation") && m.isSourceLoaded("basin");
      } catch {
        return false;
      }
    },
    undefined,
    { timeout: 90_000 },
  );
  await page.waitForTimeout(2500);
  await page.screenshot({ path: "test-results/map-overview.png" });

  // hover the densest visible compilation collar near the centre of the viewport
  const target = await page.evaluate(() => {
    type Feat = { id?: string | number; geometry: { coordinates: [number, number] } };
    type Map = {
      queryRenderedFeatures: (o: { layers: string[] }) => Feat[];
      getCanvas: () => HTMLCanvasElement;
      project: (c: [number, number]) => { x: number; y: number };
    };
    const m = (window as unknown as { __ue: { map: Map } }).__ue.map;
    const feats = m.queryRenderedFeatures({ layers: ["compilation-dot"] });
    const w = m.getCanvas().clientWidth;
    const h = m.getCanvas().clientHeight;
    let best = null;
    let bestD = Number.POSITIVE_INFINITY;
    for (const f of feats) {
      const p = m.project(f.geometry.coordinates);
      const d = Math.hypot(p.x - w * 0.6, p.y - h * 0.5);
      if (p.x > 340 && p.y > 120 && d < bestD) {
        bestD = d;
        best = { x: p.x, y: p.y, id: f.id, count: feats.length };
      }
    }
    return best;
  });
  expect(target).not.toBeNull();
  if (target === null) throw new Error("no compilation collar in view");
  await page.mouse.move(target.x, target.y);
  await expect(page.locator('[data-strict="hovercard"]')).toBeVisible({ timeout: 5000 });
  await expect(page.locator('[data-strict="hovercard"]')).toContainText(
    "Assay values: none in provincial tables",
  );
  await page.screenshot({ path: "test-results/map-hover.png" });

  await page.mouse.click(target.x, target.y);
  await expect(page.locator('[data-strict="hole-panel"]')).toBeVisible({ timeout: 5000 });
  await page.waitForTimeout(500);
  await page.screenshot({ path: "test-results/map-selected.png" });
  expect(page.url()).toContain("s=cmp:");

  // every digit inside strict regions sits in a value, identifier or instrument marker
  const unbacked = await page.evaluate(() => {
    const bad: string[] = [];
    for (const region of Array.from(document.querySelectorAll("[data-strict]"))) {
      const walker = document.createTreeWalker(region, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (!/\d/.test(n.textContent ?? "")) continue;
        const el = n.parentElement;
        if (
          el?.closest(
            "[data-vid],[data-ident],[data-instrument],[data-axis],[data-chrome],[data-source-text]",
          )
        )
          continue;
        bad.push((n.textContent ?? "").trim());
      }
    }
    return bad;
  });
  expect(unbacked).toEqual([]);
  expect(errors.filter((e) => !/Download the React DevTools/.test(e))).toEqual([]);
});
