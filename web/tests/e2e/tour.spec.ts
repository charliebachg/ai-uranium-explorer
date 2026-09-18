import { expect, test } from "@playwright/test";

const STEPS = ["title", "district", "evidence", "scores", "nullmodel", "eval", "ask", "limits"];

/** The walkthrough from research report 05, driven end to end: every step sets up its own screen. */
test("guided tour walks every step and leaves the app on the limits page", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    if (m.type() === "error") errors.push(m.text());
  });

  await page.goto("/?motion=0");
  await expect(page.getByTestId("intro")).toHaveCount(0); // a deep link never opens the intro
  await page.getByRole("button", { name: "Tour" }).click();

  const hud = page.getByTestId("tour");
  await expect(hud).toBeVisible();

  for (const [i, id] of STEPS.entries()) {
    await expect(hud).toHaveAttribute("data-step", id, { timeout: 20_000 });
    await expect(hud).toContainText(`step ${i + 1}/${STEPS.length}`);
    await page.waitForTimeout(i === 0 ? 800 : 1800); // let the step's camera and panels settle
    await page.screenshot({ path: `test-results/tour-${i + 1}-${id}.png` });

    if (id === "district") await expect(page.locator('[data-strict="hole-panel"]')).toBeVisible();
    if (id === "evidence") await expect(page.locator('[data-strict="report-panel"]')).toBeVisible();
    if (id === "scores") {
      await expect(page.getByTestId("score-legend")).toBeVisible({ timeout: 60_000 });
      await expect(page.getByTestId("honesty-banner")).toHaveAttribute("data-scores", "");
    }
    if (id === "nullmodel") await expect(page.getByTestId("score-legend")).toContainText("effort leads");
    if (id === "eval") await expect(page.locator('[data-strict="eval"]')).toBeVisible();
    if (id === "ask") {
      // the agent step opens the rail on the recorded cell and replays what was actually said
      await expect(page.getByTestId("agent-rail")).toBeVisible();
      await expect(page.getByTestId("chat-panel")).toHaveAttribute("data-replay", "");
      await expect(page.getByTestId("score-legend")).toBeVisible();
    }
    if (id === "limits") await expect(page.locator('[data-strict="limits"]')).toBeVisible();

    if (i < STEPS.length - 1) await hud.getByRole("button", { name: "Next" }).click();
  }

  // every digit on screen at the end of the tour still resolves to a stored value, identifier or instrument
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

  await hud.getByRole("button", { name: "Finish" }).click();
  await expect(page.getByTestId("tour")).toHaveCount(0);
  expect(errors.filter((e) => !/Download the React DevTools/.test(e))).toEqual([]);
});

test("tour is keyboard driven and leaves on escape", async ({ page }) => {
  await page.goto("/?motion=0");
  await page.keyboard.press("g");
  const hud = page.getByTestId("tour");
  await expect(hud).toHaveAttribute("data-step", "title");
  await page.keyboard.press("ArrowRight");
  await expect(hud).toHaveAttribute("data-step", "district");
  await page.keyboard.press("ArrowLeft");
  await expect(hud).toHaveAttribute("data-step", "title");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("tour")).toHaveCount(0);
});
