import { expect, type Page, test } from "@playwright/test";

const MAP = "/?intro=0&motion=0&c=-105.6,57.9,7.4,0,0";

async function mapReady(page: Page) {
  await page.waitForFunction(
    () => {
      const m = (window as unknown as { __lr?: { map?: { isSourceLoaded: (s: string) => boolean } } }).__lr
        ?.map;
      try {
        return !!m && m.isSourceLoaded("compilation");
      } catch {
        return false;
      }
    },
    undefined,
    { timeout: 90_000 },
  );
}

const drawnCollars = (page: Page) =>
  page.evaluate(() => {
    const m = (window as unknown as { __lr: { map: { queryRenderedFeatures: (o: unknown) => unknown[] } } })
      .__lr.map;
    return m.queryRenderedFeatures({ layers: ["compilation-dot"] }).length;
  });

test("timeline filters the provincial holes by year and says what it hides", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(MAP);
  await mapReady(page);
  await page.waitForTimeout(1500);

  const before = await drawnCollars(page);
  expect(before).toBeGreaterThan(50);

  await page.getByRole("button", { name: "Timeline" }).click();
  const hud = page.getByTestId("timeline");
  await expect(hud).toBeVisible();
  await expect(hud).toContainText("Where people drilled, by year");
  await expect(hud).toContainText("carry no drilling year");

  // scrub to about a third along the axis: fewer collars must remain drawn
  const plot = hud.getByRole("slider");
  const box = (await plot.boundingBox()) as { x: number; y: number; width: number; height: number };
  await page.mouse.move(box.x + box.width * 0.33, box.y + box.height * 0.5);
  await page.mouse.down();
  await page.mouse.up();
  await page.waitForTimeout(900);
  const after = await drawnCollars(page);
  expect(after).toBeLessThan(before);
  await page.screenshot({ path: "test-results/hud-timeline.png" });

  // keyboard scrubbing moves one year at a time
  const yearAt = () => plot.getAttribute("aria-valuenow");
  const year = Number(await yearAt());
  await plot.press("ArrowRight");
  expect(Number(await yearAt())).toBe(year + 1);

  // playback runs and settles at the end of the range
  await hud.getByRole("button", { name: /play/i }).click();
  await page.waitForTimeout(11_000);
  expect(Number(await yearAt())).toBeGreaterThan(year + 1);
  expect(await drawnCollars(page)).toBeGreaterThan(after);

  const unbacked = await page.evaluate(() => {
    const bad: string[] = [];
    for (const region of Array.from(document.querySelectorAll("[data-strict]"))) {
      const walker = document.createTreeWalker(region, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (!/\d/.test(n.textContent ?? "")) continue;
        if (
          n.parentElement?.closest(
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

  await hud.getByRole("button", { name: /close/i }).click();
  await expect(page.getByTestId("timeline")).toHaveCount(0);
  await page.waitForTimeout(600);
  expect(await drawnCollars(page)).toBe(before);
  expect(errors).toEqual([]);
});

test("command palette finds a report and toggles a layer", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto(MAP);
  await mapReady(page);

  await page.keyboard.press("ControlOrMeta+k");
  const palette = page.getByTestId("palette");
  await expect(palette).toBeVisible();
  await page.screenshot({ path: "test-results/hud-palette.png" });

  await page.keyboard.type("74H09");
  await expect(palette.getByText("74H09-0039").first()).toBeVisible();
  await page.keyboard.press("Enter");
  await expect(page.getByTestId("palette")).toHaveCount(0);
  await expect(page.locator('[data-strict="report-panel"]')).toContainText("74H09-0039");

  // a layer command changes the map, not just the list
  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.type("GeoDS drillholes");
  await page.keyboard.press("Enter");
  await page.waitForTimeout(500);
  const geodsVisible = await page.evaluate(() => {
    const m = (
      window as unknown as {
        __lr: { map: { getLayoutProperty: (l: string, p: string) => string | undefined } };
      }
    ).__lr.map;
    return m.getLayoutProperty("geods-dot", "visibility");
  });
  expect(geodsVisible).toBe("none");

  await page.keyboard.press("ControlOrMeta+k");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("palette")).toHaveCount(0);
  expect(errors).toEqual([]);
});
