import { expect, type Page, test } from "@playwright/test";
import { prospectBanner } from "../../src/config/wording";

/**
 * The score cells and the agent, on the one map. There is no separate prospect page any more: the question a
 * visitor asks is "what is going on there", and it is answered beside the place rather than on another screen.
 *
 * The local agent service may or may not be running, so every check here either works both ways or blocks the
 * service explicitly.
 */

const SERVICE = "http://127.0.0.1:8787/**";

/** Every digit on the page has to sit inside something that says where the number came from. */
async function unbackedDigits(page: Page): Promise<string[]> {
  return page.evaluate(() => {
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
}

/** The cell the recorded conversation is about: a real cell, so a click at the map centre lands on one. */
const RECORDED_CELL = { lon: -103.7255, lat: 58.1579 };

async function showScores(page: Page, camera = "") {
  await page.goto(`/?intro=0${camera}`);
  await expect(page.getByTestId("agent-rail")).toBeVisible({ timeout: 30_000 });
  await page.getByTestId("show-scores").click();
  await expect(page.getByTestId("score-legend")).toBeVisible({ timeout: 60_000 });
}

test("the score cells go on the map, and the banner changes to say colour now carries a score", async ({
  page,
}) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));

  await page.goto("/?intro=0");
  // with the cells off, the standing promise holds: colour is a source or a status
  await expect(page.getByTestId("honesty-banner")).not.toHaveAttribute("data-scores", "");

  await page.getByTestId("show-scores").click();
  await expect(page.getByTestId("honesty-banner")).toHaveAttribute("data-scores", "");
  await expect(page.getByTestId("honesty-banner")).toContainText(prospectBanner);

  // the legend is what keeps the ramp from being read as good ground and bad ground
  const legend = page.getByTestId("score-legend");
  await expect(legend).toBeVisible({ timeout: 60_000 });
  await expect(legend).toContainText("a gap, not a low score");

  await page.getByTestId("score-model-difference").click();
  await expect(legend).toContainText("effort leads");
  await expect(legend).toContainText("learned leads");

  await page.getByTestId("score-model-effort").click();
  await expect(legend).toContainText("effort score");

  expect(errors).toEqual([]);
});

test("clicking a cell opens its evidence and a conversation about it", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await showScores(page);

  // the service answers or it does not; either way, wait for the rail to have settled before deciding
  await Promise.race([
    page
      .getByTestId("ranked-cell")
      .first()
      .waitFor({ timeout: 20_000 })
      .catch(() => undefined),
    page
      .locator('[data-testid="service-offline"]')
      .first()
      .waitFor({ timeout: 20_000 })
      .catch(() => undefined),
  ]);
  if ((await page.getByTestId("ranked-cell").count()) === 0) {
    test.skip(true, "the local agent service is not running");
  }
  await page.getByTestId("ranked-cell").first().click();

  const rail = page.getByTestId("agent-rail");
  await expect(rail).toContainText(/\d{4}_\d{4}/);
  await expect(page.locator('[data-testid="criterion-row"]').first()).toBeVisible({ timeout: 60_000 });
  expect(await page.locator('[data-testid="score-row"]').count()).toBe(3);

  // unknown is its own state, never folded into "not met"
  await expect(page.locator('[data-testid="criterion-row"][data-state="unknown"]').first()).toContainText(
    "unknown",
  );
  // evidence and the conversation are two tabs over one cell, not two things sharing the height
  await page.getByTestId("tab-chat").click();
  await expect(page.getByTestId("chat-panel")).toContainText("checked against the tool values");
  await expect(page.getByTestId("chat-input")).toBeVisible();
  await expect(page.locator('[data-testid="criterion-row"]')).toHaveCount(0);
  await page.getByTestId("tab-evidence").click();
  await expect(page.locator('[data-testid="criterion-row"]').first()).toBeVisible();

  expect(await unbackedDigits(page)).toEqual([]);
  expect(errors).toEqual([]);
});

test("with the service down the cells still draw and both panels say why", async ({ page }) => {
  const errors: string[] = [];
  page.on("pageerror", (e) => errors.push(e.message));
  await page.route(SERVICE, (route) => route.abort());
  await showScores(page, `&c=${RECORDED_CELL.lon},${RECORDED_CELL.lat},9`);

  // no ranked list without the service, and the rail says what to start
  await expect(page.getByTestId("ranked-cell")).toHaveCount(0);
  await expect(page.getByTestId("agent-empty")).toContainText("Click a cell");

  // The legend appears the moment the layer is switched on, but the cell file is several megabytes and the
  // click has nothing to land on until it has drawn. Wait for a cell to actually be under the cursor.
  const canvas = page.locator(".maplibregl-canvas").first();
  const box = await canvas.boundingBox();
  if (!box) throw new Error("the map has no box");
  // A hole read from a report may sit on top of the centre cell since the reading pass, and the hovercard
  // names the hole first; probe a few offsets so the cursor lands on the cell itself.
  const centre = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
  const offsets: [number, number][] = [[0, 0], [24, 0], [0, 24], [-24, 0], [0, -24], [24, 24], [-24, -24]];
  let hit = centre;
  await expect(async () => {
    for (const [dx, dy] of offsets) {
      const at = { x: centre.x + dx, y: centre.y + dy };
      await page.mouse.move(at.x, at.y - 1);
      await page.mouse.move(at.x, at.y);
      const label = await page.getByTestId("hovercard-label").textContent({ timeout: 1_000 }).catch(() => "");
      if (label === "Analysis cell") {
        hit = at;
        return;
      }
    }
    throw new Error("no analysis cell under the cursor at any probed offset");
  }).toPass({ timeout: 90_000 });
  await page.mouse.click(hit.x, hit.y);

  // the exported row still says what the map drew, and the service-backed panels explain themselves
  await expect(page.getByTestId("map-scores")).toBeVisible({ timeout: 30_000 });
  await expect(page.locator('[data-testid="service-offline"]').first()).toContainText("ue prospect serve");

  expect(await unbackedDigits(page)).toEqual([]);
  expect(errors).toEqual([]);
});

test("the walkthrough replays a real conversation, withheld answer included", async ({ page }) => {
  await page.route(SERVICE, (route) => route.abort());
  await page.goto("/?intro=0");
  await expect(page.getByTestId("agent-rail")).toBeVisible({ timeout: 30_000 });
  await page.keyboard.press("g");
  const hud = page.getByTestId("tour");
  await expect(hud).toHaveAttribute("data-step", "title", { timeout: 30_000 });
  // Walk to the agent step by name, so adding or dropping a step ahead of it cannot silently overshoot.
  // Each press waits for the step to actually change: the score steps fetch several megabytes of cells, and
  // a keypress sent while that is parsing is simply lost.
  for (let i = 0; i < 12; i++) {
    const at = await hud.getAttribute("data-step");
    if (at === "ask") break;
    await page.keyboard.press("ArrowRight");
    await expect.poll(() => hud.getAttribute("data-step"), { timeout: 30_000 }).not.toBe(at);
  }
  await expect(hud).toHaveAttribute("data-step", "ask", { timeout: 30_000 });
  await expect(page.getByTestId("chat-panel")).toHaveAttribute("data-replay", "", { timeout: 30_000 });
  await expect(page.getByTestId("chat-panel")).toContainText("recorded");
  // the gate's own record: a turn it refused is kept, with what it objected to
  const turns = page.locator('[data-testid="chat-turn"]');
  expect(await turns.count()).toBeGreaterThan(0);
  expect(await unbackedDigits(page)).toEqual([]);
});

test("the evidence layers are grouped like MineTRACE, deferred, and honest about what is missing", async ({
  page,
}) => {
  // either transport counts: the GeoJSON fallback, or tile bytes past the archive header
  const fetched: string[] = [];
  page.on("request", (r) => {
    const url = r.url();
    if (/\/data\/context\/(em_conductors|faults|graphitic_host)\.geojson/.test(url)) fetched.push(url);
    if (/\/data\/tiles\/(conductors|faults|host)\.pmtiles/.test(url)) {
      const range = r.headers().range ?? "";
      const start = Number(/bytes=(\d+)-/.exec(range)?.[1] ?? 0);
      if (start >= 16384) fetched.push(`${url} ${range}`);
    }
  });
  await page.goto("/?intro=0");
  await expect(page.getByTestId("agent-rail")).toBeVisible({ timeout: 30_000 });
  await page.waitForTimeout(1500);

  // 16 MB of evidence must not be fetched by a reader who never opens it
  expect(fetched, "evidence layers were fetched before anyone asked for them").toEqual([]);

  const rail = page.getByRole("complementary", { name: "Layers" });
  for (const group of ["Pathway and trap", "Geochemistry", "Where people already looked", "Geophysics"]) {
    await expect(rail).toContainText(group);
  }

  // the three grids this ground does not publish are named, not silently absent
  const gaps = page.getByTestId("layer-gap");
  expect(await gaps.count()).toBe(3);
  await expect(gaps.first()).toContainText("no public grid");

  await rail.getByText("EM conductors", { exact: false }).click();
  await expect
    .poll(() => fetched.length, { timeout: 60_000, message: "the layer was not fetched on first use" })
    .toBeGreaterThan(0);
});
