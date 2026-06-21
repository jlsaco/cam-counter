import { expect, test } from "@playwright/test";

// E2E: la fuente falsa reproduce cruces guionizados (smooth_crossing_script) que
// recorren el pipeline REAL (DummyDetector -> tracker -> LineCounter -> Store) y
// emiten WsEnvelope; la SPA, suscrita al hub WS, refresca el contador EN VIVO.
//
// Robusto al sentido: se asierta que el TOTAL de cruces (subieron + bajaron)
// crece, así el test no depende de que la línea esté en una orientación concreta
// (otro spec puede haber invertido el sentido sobre el mismo servidor).

const CAM = "demo-pi-cam0";

async function readTotal(page: import("@playwright/test").Page): Promise<number> {
  const inText = (await page.getByText(/subieron:\s*\d+/).first().textContent()) ?? "";
  const outText = (await page.getByText(/bajaron:\s*\d+/).first().textContent()) ?? "";
  const inN = Number(inText.match(/(\d+)/)?.[1] ?? "0");
  const outN = Number(outText.match(/(\d+)/)?.[1] ?? "0");
  return inN + outN;
}

test("el cruce guionizado incrementa el contador en vivo por WS", async ({ page }) => {
  await page.goto("/");
  await page.getByRole("button", { name: new RegExp(CAM) }).click();

  // El panel de contadores en vivo está visible (subieron/bajaron/net).
  await expect(page.getByText(/subieron:\s*\d+/).first()).toBeVisible();

  const initial = await readTotal(page);
  // La fuente falsa produce ~1 cruce por bucle; el WS dispara el refresco del
  // contador en la UI (sin recargar la página).
  await expect
    .poll(() => readTotal(page), { timeout: 25_000, intervals: [400] })
    .toBeGreaterThan(initial);
});
