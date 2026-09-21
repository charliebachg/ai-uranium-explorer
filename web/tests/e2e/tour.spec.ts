import { expect, test } from "@playwright/test";

const STEPS = ["title", "data", "map", "scores", "nullmodel", "eval", "chains", "ask", "analyst", "limits"];

/**
 * The walkthrough, driven end to end: every step sets up its own screen. The local service is optional by
 * design, so the steps that read from it (the chains) are asserted either way: the chain when it is up, the
 * offline notice when it is not. The recorded session needs no service at all.
 */
test("guided tour walks every step and leaves the app on the limits page", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  page.on("console", (m) => {
    // the chat service on :8787 is optional by design; the browser logs its refused connection as an error
    if (m.type() === "error" && !m.location().url.includes(":8787/")) errors.push(m.text());
  });

  await page.goto("/?motion=0");
  await expect(page.getByTestId("intro")).toHaveCount(0); // a deep link never opens the intro
  await page.getByRole("button", { name: "Tour" }).click();

  const hud = page.getByTestId("tour");
  await expect(hud).toBeVisible();

  // the eval blocks are absent, not empty, until their exports have been rebuilt; asserted only when carried
  const carried = await page.evaluate(async () => {
    const res = await fetch("/data/prospect/readiness.json");
    const doc = (await res.json()) as { search?: { rows?: unknown[] }; bench?: { rows?: unknown[] } };
    return { search: Boolean(doc.search?.rows?.length), bench: Boolean(doc.bench?.rows?.length) };
  });

  for (const [i, id] of STEPS.entries()) {
    await expect(hud).toHaveAttribute("data-step", id, { timeout: 20_000 });
    await expect(hud).toContainText(`step ${i + 1}/${STEPS.length}`);
    await page.waitForTimeout(i === 0 ? 800 : 1800); // let the step's camera and panels settle
    await page.screenshot({ path: `test-results/tour-${i + 1}-${id}.png` });

    if (id === "data") {
      const readiness = page.locator('[data-strict="readiness"]');
      await expect(readiness).toBeVisible();
      await expect(readiness).toContainText("Coverage, not prospectivity");
    }
    if (id === "map") {
      // the enabled cell is open in the rail, by its id
      const rail = page.getByTestId("agent-rail");
      await expect(rail).toBeVisible();
      await expect(rail.locator("[data-ident]").first()).toHaveText(/^\d{4}_\d{4}$/);
    }
    if (id === "scores") {
      await expect(page.getByTestId("score-legend")).toBeVisible({ timeout: 60_000 });
      await expect(page.getByTestId("honesty-banner")).toHaveAttribute("data-scores", "");
    }
    if (id === "nullmodel") await expect(page.getByTestId("score-legend")).toContainText("effort leads");
    if (id === "eval") {
      const evalPage = page.locator('[data-strict="eval"]');
      await expect(evalPage).toBeVisible();
      await expect(evalPage.locator('[data-testid="hindcast-table"]')).toBeVisible();
      if (carried.search) await expect(evalPage.locator('[data-testid="search-table"]')).toBeVisible();
      if (carried.bench) await expect(evalPage.locator('[data-testid="bench-table"]')).toBeVisible();
    }
    if (id === "chains") {
      // the evidence tab on the recorded cell: the stored chain with the service up, the notice without it
      await expect(page.getByTestId("agent-rail")).toBeVisible();
      await expect(page.getByTestId("tab-panel-evidence")).toBeVisible();
      await expect(
        page.locator('[data-testid="chain"], [data-testid="service-offline"]').first(),
      ).toBeVisible({ timeout: 30_000 });
      await expect(page.getByTestId("score-legend")).toBeVisible();
    }
    if (id === "ask") {
      // the agent step opens the rail on the recorded cell and replays what was actually said, up to the
      // question the system must refuse; the analyst is not invoked yet
      await expect(page.getByTestId("agent-rail")).toBeVisible();
      const panel = page.getByTestId("chat-panel");
      await expect(panel).toHaveAttribute("data-replay", "", { timeout: 30_000 });
      await expect(panel).toContainText("recorded");
      expect(await panel.locator('[data-testid="chat-route"]').count()).toBeGreaterThan(0);
      await expect(panel.locator('[data-testid="chat-abstention"]').first()).toBeVisible();
      expect(await panel.locator('[data-testid="chat-turn"] [data-vid]').count()).toBeGreaterThan(0);
      await expect(panel.locator('[data-testid="chat-job"]')).toHaveCount(0);
    }
    if (id === "analyst") {
      // the whole session: the job card as its row ended, and the follow-up carrying the diff
      const panel = page.getByTestId("chat-panel");
      await expect(panel).toHaveAttribute("data-replay", "", { timeout: 30_000 });
      const card = panel.locator('[data-testid="chat-job"]').first();
      await expect(card).toBeVisible();
      await expect(card).toHaveAttribute("data-status", "done");
      await expect(card.locator('[data-testid="chat-job-stages"]')).toBeVisible();
      await expect(panel.locator('[data-testid="chat-job-done"]')).toBeVisible();
      await expect(panel.locator('[data-testid="chat-assessment"]')).toBeVisible();
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
  await expect(hud).toHaveAttribute("data-step", "data");
  await page.keyboard.press("ArrowLeft");
  await expect(hud).toHaveAttribute("data-step", "title");
  await page.keyboard.press("Escape");
  await expect(page.getByTestId("tour")).toHaveCount(0);
});
