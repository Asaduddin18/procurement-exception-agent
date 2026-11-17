import { defineConfig } from "@playwright/test";

const externalUrl = process.env.PLAYWRIGHT_BASE_URL;

export default defineConfig({
  testDir: "./tests",
  workers: 1,
  timeout: 75_000,
  expect: { timeout: 15_000 },
  use: {
    baseURL: externalUrl ?? "http://127.0.0.1:3000",
    viewport: { width: 1440, height: 1080 },
    screenshot: "only-on-failure",
    trace: "retain-on-failure",
  },
  webServer: externalUrl ? undefined : {
    command: "npm run start",
    url: "http://127.0.0.1:3000",
    timeout: 30000,
  },
});
