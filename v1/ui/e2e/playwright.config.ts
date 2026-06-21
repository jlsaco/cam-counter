import os from "node:os";
import path from "node:path";
import { defineConfig, devices } from "@playwright/test";

// E2E contra la API local sirviendo la SPA con la FUENTE FALSA determinista
// (CAMCOUNTER_FAKE_SOURCE=1): vídeo + cruces reproducibles sin Pi/Hailo/cámara.
// `webServer` arranca Uvicorn (asume `v1/ui/dist` ya construido y el paquete de
// borde + deps de la API instalados). El puerto es configurable por entorno
// (CAMCOUNTER_PORT, def 8000) para evitar choques en runners ocupados.
const PORT = Number(process.env.CAMCOUNTER_PORT ?? 8000);

// SQLite FRESCO por corrida: cada `playwright test` arranca con una DB limpia
// (la fuente falsa crea esquema + config por defecto), para aserciones
// deterministas (versiones de config, contadores que arrancan en 0).
const DB_PATH =
  process.env.CAMCOUNTER_DB_PATH ??
  path.join(os.tmpdir(), `cam-counter-e2e-${process.pid}.db`);

export default defineConfig({
  testDir: ".",
  timeout: 30_000,
  // Servidor único con estado (SQLite + fuente falsa): sin paralelismo entre
  // specs para evitar carreras sobre la misma DB/config.
  fullyParallel: false,
  workers: 1,
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `python -m uvicorn app:app --host 127.0.0.1 --port ${PORT}`,
    cwd: "../../api",
    env: {
      CAMCOUNTER_FAKE_SOURCE: "1",
      CAMCOUNTER_PORT: String(PORT),
      CAMCOUNTER_DB_PATH: DB_PATH,
      // Cadencia rápida de la fuente falsa: cruces y MJPEG más ágiles para E2E.
      CAMCOUNTER_FRAME_INTERVAL: "0.05",
    },
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
