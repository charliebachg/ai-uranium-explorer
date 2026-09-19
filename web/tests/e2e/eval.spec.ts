import { expect, test } from "@playwright/test";

test("eval page reports run statistics and refuses to call them accuracy", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/eval");
  const evalPage = page.locator('[data-strict="eval"]');
  await expect(evalPage).toContainText("No gold labels yet: these are run statistics, not accuracy");
  await expect(evalPage).toContainText("V01");
  await expect(evalPage).toContainText("What would turn these into accuracy");
  // the tracked evaluations: every row of the hindcast names its run, and the table is on the page
  await expect(evalPage.locator('[data-testid="hindcast-table"]')).toBeVisible();
  expect(await evalPage.locator('[data-testid="hindcast-row"]').count()).toBeGreaterThan(5);
  await page.waitForTimeout(800);
  await page.screenshot({ path: "test-results/eval-page.png", fullPage: true });

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

  // a flagged example jumps to the page it came from
  await evalPage
    .getByRole("button", { name: /74H09-0039/ })
    .first()
    .click();
  await expect(page.locator('[data-strict="page-viewer"]')).toBeVisible({ timeout: 10_000 });
  await page.waitForTimeout(1200);
  await page.screenshot({ path: "test-results/eval-to-page.png" });
  expect(errors).toEqual([]);
});

test("limits page states what the demo cannot claim", async ({ page }) => {
  await page.goto("/limits");
  const limits = page.locator('[data-strict="limits"]');
  await expect(limits).toContainText("What this demo can and cannot claim");
  await expect(limits).toContainText("Drill here, or this saves holes");
  await expect(limits).toContainText("I am not a geologist");
  await page.screenshot({ path: "test-results/limits-page.png", fullPage: true });
});
