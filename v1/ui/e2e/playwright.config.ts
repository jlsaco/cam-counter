import { defineConfig, devices } from "@playwright/test";

// E2E contra la API local sirviendo la SPA con la FUENTE FALSA determinista
// (CAMCOUNTER_FAKE_SOURCE=1): vídeo + cruces reproducibles sin Pi/Hailo/cámara.
// `webServer` arranca Uvicorn (asume `v1/ui/dist` ya construido y el paquete de
// borde + deps de la API instalados). El puerto es configurable por entorno
// (CAMCOUNTER_PORT, def 8000) para evitar choques en runners ocupados. La suite
// completa llega en PR10.
const PORT = Number(process.env.CAMCOUNTER_PORT ?? 8000);

export default defineConfig({
  testDir: ".",
  timeout: 30_000,
  use: {
    baseURL: `http://127.0.0.1:${PORT}`,
    trace: "on-first-retry",
  },
  projects: [{ name: "chromium", use: { ...devices["Desktop Chrome"] } }],
  webServer: {
    command: `python -m uvicorn app:app --host 127.0.0.1 --port ${PORT}`,
    cwd: "../../api",
    env: { CAMCOUNTER_FAKE_SOURCE: "1", CAMCOUNTER_PORT: String(PORT) },
    url: `http://127.0.0.1:${PORT}/api/health`,
    reuseExistingServer: !process.env.CI,
    timeout: 30_000,
  },
});
