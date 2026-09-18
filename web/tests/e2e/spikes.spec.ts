import { expect, test } from "@playwright/test";

type Result = { name: string; ok: boolean | null; detail: string };

test("phase 0 map spikes pass in a real browser", async ({ page }) => {
  const consoleErrors: string[] = [];
  page.on("console", (m) => {
    if (m.type() === "error") consoleErrors.push(m.text());
  });
  await page.goto("/?spike=1");
  await page.waitForFunction(() => window.__spike?.done === true, undefined, { timeout: 110_000 });
  const results = (await page.evaluate(() => window.__spike?.results ?? [])) as Result[];
  for (const r of results) console.log(`${r.ok ? "PASS" : "FAIL"}  ${r.name}  ::  ${r.detail}`);
  await page.screenshot({ path: "test-results/spikes.png" });
  expect(results.length).toBeGreaterThanOrEqual(8);
  expect(results.filter((r) => !r.ok).map((r) => `${r.name}: ${r.detail}`)).toEqual([]);
  expect(consoleErrors).toEqual([]);
});
