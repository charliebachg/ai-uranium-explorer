import { defineConfig, devices } from "@playwright/test";

// Headless Chromium runs requestAnimationFrame normally (a hidden desktop tab does not), so map checks are
// automated here; visual QA uses screenshots in a real Chrome window.
export default defineConfig({
  testDir: "tests/e2e",
  timeout: 120_000,
  fullyParallel: false,
  // these specs load live basemap and terrain tiles; under parallel load a throttled tile can fail a run that
  // passes on its own, so one retry separates a flaky network from a real regression
  retries: 1,
  reporter: [["list"]],
  use: {
    baseURL: "http://localhost:5173",
    ...devices["Desktop Chrome"],
    viewport: { width: 1440, height: 900 },
    launchOptions: { args: ["--use-angle=swiftshader", "--enable-unsafe-swiftshader", "--ignore-gpu-blocklist"] },
  },
  webServer: { command: "npm run dev", url: "http://localhost:5173", reuseExistingServer: true, timeout: 60_000 },
});
