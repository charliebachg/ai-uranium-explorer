import { expect, test } from "@playwright/test";

test("data readiness page reports coverage, not a ranking of ground", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/data");

  const readiness = page.locator('[data-strict="readiness"]');
  await expect(readiness).toBeVisible();
  await expect(readiness).toContainText("Coverage, not prospectivity");

  // the coverage table is the centre of the page: one row per feature, geological first
  const rows = readiness.locator('[data-testid="feature-row"]');
  await expect(rows.first()).toBeVisible();
  expect(await rows.count()).toBeGreaterThanOrEqual(15);

  // the thin features are marked, so a reader sees at a glance which ones cover a minority of the grid
  const thin = readiness.locator("[data-thin]");
  expect(await thin.count()).toBeGreaterThan(0);
  await expect(thin.first()).toContainText("thin");

  // effort features are separated from geology and named as the null model
  await expect(readiness).toContainText("Exploration-effort features");
  await expect(readiness).toContainText("null model");

  // the gaps section names the geophysics that is not public at all
  await expect(readiness).toContainText("What is missing");
  await expect(readiness).toContainText("Airborne magnetic, radiometric and gravity grids");
  await expect(readiness).toContainText("Open Government Licence");
  // the five-column gate is on the page, with a verdict and one row per dataset
  await expect(readiness.locator('[data-testid="gate-table"]')).toBeVisible();
  await expect(readiness.locator('[data-testid="gate-verdict"]')).toContainText(/gate (green|red)/);
  expect(await readiness.locator('[data-testid="gate-row"]').count()).toBeGreaterThan(10);

  // sources: labels are flagged as labels, and an unverified source is marked
  await expect(readiness).toContainText("never used as a feature");
  await expect(readiness).toContainText("not verified");

  // the cell file is only fetched when asked for; it then draws
  const canvas = readiness.locator('[data-testid="coverage-map-canvas"]');
  await expect(canvas).toHaveCount(0);
  await readiness.getByTestId("coverage-map-request").click();
  await expect(canvas).toBeVisible({ timeout: 30_000 });
  const painted = await canvas.evaluate((el) => {
    const c = el as HTMLCanvasElement;
    const ctx = c.getContext("2d");
    if (!ctx) return 0;
    const { data } = ctx.getImageData(0, 0, c.width, c.height);
    let lit = 0;
    for (let i = 3; i < data.length; i += 4) if ((data[i] ?? 0) > 0) lit++;
    return lit;
  });
  expect(painted).toBeGreaterThan(1000);

  // every digit on the page resolves to a stored value, an identifier or an instrument reading
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
        bad.push((n.textContent ?? "").trim().slice(0, 60));
      }
    }
    return bad;
  });
  expect(unbacked).toEqual([]);

  // the page scrolls inside its own container, so fullPage cannot reach past it: rewind to the top first
  await readiness.evaluate((el) => {
    el.scrollTop = 0;
  });
  await page.screenshot({ path: "test-results/readiness.png", fullPage: true });
  expect(errors).toEqual([]);
});

test("the data view is reachable from the top bar", async ({ page }) => {
  await page.goto("/?intro=0"); // the opening sequence has its own spec
  await page.getByRole("navigation", { name: "Views" }).getByRole("link", { name: "Data" }).click();
  await expect(page.locator('[data-testid="readiness"]')).toBeVisible();
  expect(new URL(page.url()).pathname).toBe("/data");
});
