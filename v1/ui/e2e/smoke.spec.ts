import { expect, test } from "@playwright/test";

// Smoke E2E mínimo (scaffold; PR10 amplía a la suite completa de la UI).
// Verifica que la SPA carga same-origin, que la cabecera muestra la identidad y
// que el stream MJPEG y la salud de producto responden con la fuente falsa.

test("la SPA carga y muestra cabecera + cámaras", async ({ page }) => {
  await page.goto("/");
  await expect(page.getByRole("heading", { name: "cam-counter" })).toBeVisible();
  // Con la fuente falsa hay al menos una cámara (demo-pi-cam0).
  await expect(page.getByRole("button", { name: /demo-pi-cam0/ })).toBeVisible();
});

test("/api/health reporta salud de producto", async ({ request }) => {
  const resp = await request.get("/api/health");
  expect(resp.ok()).toBeTruthy();
  const body = await resp.json();
  expect(body).toHaveProperty("db_schema_version");
  expect(Array.isArray(body.cameras)).toBeTruthy();
});

test("el stream MJPEG responde como multipart", async ({ request }) => {
  const resp = await request.get("/api/cameras/demo-pi-cam0/stream.mjpg?frames=1");
  expect(resp.ok()).toBeTruthy();
  expect(resp.headers()["content-type"]).toContain("multipart/x-mixed-replace");
});
