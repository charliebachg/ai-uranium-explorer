import { expect, test } from "@playwright/test";

// Visual QA only (screenshots for review); run with: npx playwright test tests/e2e/qa.spec.ts
const VIEWS: [string, string][] = [
  ["qa-eastern-z9", "/?c=-105.05,57.78,9.3,0,0&b=ink&L=rel,basin,dep,gds,cmp"],
  ["qa-rabbit-z12-pitch", "/?c=-103.72,58.21,12.2,-20,45&b=ink&L=rel,basin,dep,gds,cmp,nts"],
  ["qa-streets-z7", "/?c=-106.5,57.4,7,0,0&b=ofm-dark&L=rel,basin,gds,cmp,occ"],
  ["qa-none-z5", "/?c=-106,57.5,5.2,0,0&b=none&L=basin,cmp,nts"],
];

for (const [name, url] of VIEWS) {
  test(name, async ({ page }) => {
    await page.goto(url);
    await page.waitForFunction(
      () => {
        const m = (
          window as unknown as {
            __ue?: { map?: { loaded: () => boolean; isSourceLoaded: (s: string) => boolean } };
          }
        ).__ue?.map;
        try {
          return !!m && m.isSourceLoaded("compilation");
        } catch {
          return false;
        }
      },
      undefined,
      { timeout: 90_000 },
    );
    await page.waitForTimeout(4000);
    await page.screenshot({ path: `test-results/${name}.png` });
  });
}

test("qa-attribution-dialog", async ({ page }) => {
  await page.goto("/?c=-106,57.5,5.2,0,0");
  await page.getByText("Data sources and licences").click();
  await page.waitForTimeout(800);
  await page.screenshot({ path: "test-results/qa-attribution.png" });
});

test("qa-datum-field", async ({ page }) => {
  await page.goto("/?c=-105.5,58.1,7.6,0,0&b=ink&L=rel,basin,cmp,dsf");
  await page.waitForFunction(
    () => {
      const m = (window as unknown as { __ue?: { map?: { isSourceLoaded: (s: string) => boolean } } }).__ue
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
  await page.waitForTimeout(3500);
  const arrows = await page.evaluate(() => {
    const m = (window as unknown as { __ue: { map: { querySourceFeatures: (s: string) => unknown[] } } }).__ue
      .map;
    return m.querySourceFeatures("datum-arrows").length;
  });
  expect(arrows).toBeGreaterThan(20);
  await page.screenshot({ path: "test-results/qa-datum-field.png" });
});
