import { expect, test } from "@playwright/test";

/** The evidence loop on real pipeline output (no fixture): the 1978 file with feet depths and a local grid. */
test("real report: feet read as metres is visible as a flagged value with its page", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/?motion=0&r=74H09-0039");
  const panel = page.locator('[data-strict="report-panel"]');
  await expect(panel).toContainText("Assessment file 74H09-0039");
  await expect(panel).toContainText("CONWEST");
  await page.waitForTimeout(2500);
  await page.screenshot({ path: "test-results/real-report.png" });

  // open the first hole and find a flagged value
  await panel.getByRole("button", { name: /R-78-/ }).first().click();
  await expect(panel).toContainText("Collar, as printed");
  await expect(panel).toContainText("not stated on page");
  await expect(panel).toContainText("not a measure of how well the page was read");
  await page.waitForTimeout(600);
  await page.screenshot({ path: "test-results/real-hole.png" });

  // any page-backed value in the panel body opens its page (this hole prints no collar fields at all)
  const clickable = panel.locator("button[data-vid]");
  expect(await clickable.count()).toBeGreaterThan(0);
  await clickable.first().click();
  const viewer = page.locator('[data-strict="page-viewer"]');
  await expect(viewer).toBeVisible();
  await expect(viewer.locator("img")).toBeVisible();
  await page.waitForTimeout(1500);
  await page.screenshot({ path: "test-results/real-page.png" });
  expect(errors).toEqual([]);
});
