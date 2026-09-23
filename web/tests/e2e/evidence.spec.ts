import { expect, type Page, test } from "@playwright/test";

async function unbackedDigits(page: Page): Promise<string[]> {
  return page.evaluate(() => {
    const bad: string[] = [];
    for (const region of Array.from(document.querySelectorAll("[data-strict]"))) {
      const walker = document.createTreeWalker(region, NodeFilter.SHOW_TEXT);
      for (let n = walker.nextNode(); n; n = walker.nextNode()) {
        if (!/\d/.test(n.textContent ?? "")) continue;
        const el = n.parentElement;
        if (
          el?.closest(
            "[data-vid],[data-ident],[data-instrument],[data-axis],[data-chrome],[data-source-text],svg title",
          )
        )
          continue;
        bad.push((n.textContent ?? "").trim());
      }
    }
    return bad;
  });
}

test("evidence loop on the fixture: report, hole, value to page box", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.goto("/?fixture=1&motion=0");
  await expect(page.getByTestId("fixture-watermark")).toBeVisible();

  // the reports are a dropdown, closed until asked for
  await page.getByTestId("reports-toggle").click();
  await page
    .getByRole("button", { name: /64L04-0075/ })
    .first()
    .click();
  const panel = page.locator('[data-strict="report-panel"]');
  await expect(panel).toContainText("Assessment file 64L04-0075");
  await page.waitForTimeout(1800);
  await page.screenshot({ path: "test-results/evidence-report.png" });

  await panel.getByRole("button", { name: /Q9-5/ }).click();
  await expect(panel).toContainText("Collar, as printed");
  await expect(panel).toContainText("not stated on page");
  await page.screenshot({ path: "test-results/evidence-hole.png" });

  // click the printed elevation: the page opens with its box highlighted
  await panel.locator('[data-vid="x:64L04-0075:q95_elev"]').click();
  const viewer = page.locator('[data-strict="page-viewer"]');
  await expect(viewer).toBeVisible();
  await expect(viewer.locator("[data-selected-box]")).toHaveCount(1);
  await expect(viewer).toContainText("ELEVATION: 434.38");
  await page.waitForTimeout(1200);
  await page.screenshot({ path: "test-results/evidence-page-elev.png" });

  // the box sits inside the viewport
  const box = await viewer.locator("[data-selected-box]").boundingBox();
  const vp = await viewer.boundingBox();
  expect(box && vp && box.x >= vp.x && box.y >= vp.y && box.x + box.width <= vp.x + vp.width).toBeTruthy();

  // J steps to the next value in the hole; an unlocated quote shows its warning
  await page.locator('[data-strict="report-panel"]').locator('[data-vid="x:64L04-0075:q95_dip"]').click();
  await expect(viewer).toContainText("not located on this page");
  await page.screenshot({ path: "test-results/evidence-page-unlocated.png" });

  // Q9-6: empty U3O8 cells read as "not printed"
  await page.keyboard.press("Escape");
  await page.keyboard.press("Escape");
  await expect(panel).toContainText("Holes read from this file");
  await panel.getByRole("button", { name: /Q9-6/ }).click();
  await expect(panel).toContainText("not printed");
  await panel.locator('[data-vid="x:64L04-0075:q96_s2_from"]').click();
  await expect(viewer.locator("[data-selected-box]")).toHaveCount(1);
  await page.waitForTimeout(1200);
  await page.screenshot({ path: "test-results/evidence-page-table.png" });

  expect(await unbackedDigits(page)).toEqual([]);
  expect(page.url()).toContain("v=x:64L04-0075:q96_s2_from");
  expect(errors).toEqual([]);
});
