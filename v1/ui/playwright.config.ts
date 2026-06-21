import { defineConfig, devices } from "@playwright/test";

// E2E de la UI local contra el harness FastAPI + FUENTE FAKE
// (`CAMCOUNTER_FAKE_SOURCE=1`). `webServer` levanta uvicorn sirviendo la SPA
// estática same-origin; no requiere Pi, cámara ni AWS. Headless (chromium).
//
// NOTA de entorno: Playwright NO publica binarios de chromium para Linux ARM64,
// así que esta suite corre en el job de CI x86 (ubuntu-latest), no en el Pi.
const PORT = process.env.CAMCOUNTER_E2E_PORT || "8099";
// Intérprete con fastapi/uvicorn instalados (en CI x86 es `python3`; en local se
// puede apuntar a un venv vía CAMCOUNTER_E2E_PYTHON).
const PY = process.env.CAMCOUNTER_E2E_PYTHON || "python3";

export default defineConfig({
  testDir: "./e2e",
  timeout: 30_000,
  expect: { timeout: 10_000 },
  fullyParallel: false,
  reporter: process.env.CI ? "list" : "line",
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `${PY} -m uvicorn app:app --host 127.0.0.1 --port ${PORT}`,
    cwd: "../api",
    url: `http://127.0.0.1:${PORT}/api/line`,
    reuseExistingServer: !process.env.CI,
    timeout: 60_000,
    env: {
      CAMCOUNTER_FAKE_SOURCE: "1",
      CAMCOUNTER_FAKE_INTERVAL: "0.3",
      CAMCOUNTER_LINE_STATE: "/tmp/cam_counter_e2e_line.json",
    },
  },
});
