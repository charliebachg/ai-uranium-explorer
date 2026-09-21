import { expect, test } from "@playwright/test";

/** The opening sequence: globe, title card, flight to the province, and the three stored counters. */
test("intro holds the globe, then flies in and settles on backed counters", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("/");
  const intro = page.getByTestId("intro");
  await expect(intro).toBeVisible();
  await expect(intro).toContainText("It makes no geological judgement, proposes no drill targets");

  // the camera waits on the globe, well outside the province
  await page.waitForFunction(
    () => {
      const m = (window as unknown as { __ue?: { map?: { getZoom: () => number } } }).__ue?.map;
      return !!m && m.getZoom() < 3;
    },
    undefined,
    { timeout: 30_000 },
  );
  await page.waitForTimeout(1200);
  await page.screenshot({ path: "test-results/intro-title.png" });

  await expect(intro).toHaveAttribute("data-phase", "title");
  await intro.getByRole("button", { name: "Begin" }).click();
  // the phase is the state that matters; the button leaving is the animation that follows it
  await expect(intro).toHaveAttribute("data-phase", /flying|counters/);
  await expect(intro.getByRole("button", { name: "Begin" })).toHaveCount(0);

  // counters appear once the flight lands, and each number is a stored value
  const counters = intro.locator("[data-vid]");
  await expect(counters.first()).toBeVisible({ timeout: 20_000 });
  expect(await counters.count()).toBe(3);
  await expect(intro).toContainText("5,822");
  await page.screenshot({ path: "test-results/intro-counters.png" });

  await page.waitForFunction(
    () => {
      const m = (window as unknown as { __ue?: { map?: { getZoom: () => number } } }).__ue?.map;
      return !!m && m.getZoom() > 5;
    },
    undefined,
    { timeout: 20_000 },
  );

  // the sequence ends on its own and the map is left interactive
  await expect(page.getByTestId("intro")).toHaveCount(0, { timeout: 20_000 });
  await expect(page.getByTestId("honesty-banner")).toBeVisible();
  expect(errors).toEqual([]);
});

test("a deep link skips the intro", async ({ page }) => {
  await page.goto("/?c=-106,57.5,5.4,0,0");
  await expect(page.getByTestId("intro")).toHaveCount(0);
  await expect(page.getByTestId("honesty-banner")).toBeVisible();
});
