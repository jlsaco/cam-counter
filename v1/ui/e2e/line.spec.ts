import { expect, test } from "@playwright/test";

// E2E del flujo de la línea de conteo contra el harness FastAPI + fuente fake:
//   cargar UI -> invertir sentido -> arrastrar la línea -> guardar -> recargar
//   (persiste) -> el cruce guionizado incrementa el contador en vivo por WS.

test("configurar la línea, persistir tras recarga e incrementar contador por WS", async ({
  page,
}) => {
  await page.goto("/");

  // La SPA carga y pinta el overlay SVG con la línea de conteo.
  await expect(page.getByTestId("line-a")).toBeVisible();
  await expect(page.getByTestId("line-coords")).toContainText("a=(");

  // --- invertir el sentido (flip): positive_side y etiquetas se intercambian ---
  const sideBefore = await page.getByTestId("positive-side").textContent();
  await page.getByTestId("btn-flip").click();
  const sideAfter = await page.getByTestId("positive-side").textContent();
  expect(sideAfter).not.toEqual(sideBefore);

  // --- arrastrar el extremo A de la línea (overlay SVG, coords normalizadas) ---
  const stage = await page.locator(".stage").boundingBox();
  const handle = await page.getByTestId("line-a").boundingBox();
  if (!stage || !handle) throw new Error("no se pudo medir el stage/handle");
  await page.mouse.move(handle.x + handle.width / 2, handle.y + handle.height / 2);
  await page.mouse.down();
  await page.mouse.move(stage.x + stage.width * 0.2, stage.y + stage.height * 0.4, {
    steps: 12,
  });
  await page.mouse.up();

  const coordsAfterDrag = await page.getByTestId("line-coords").textContent();

  // --- guardar: config_version MONÓTONO sube; el estado se persiste en local ---
  await page.getByTestId("btn-save").click();
  await expect(page.getByTestId("save-status")).toContainText("guardado v");
  const savedCoords = await page.getByTestId("line-coords").textContent();
  const savedSide = await page.getByTestId("positive-side").textContent();
  const savedVersion = await page.getByTestId("config-version").textContent();
  expect(savedCoords).toEqual(coordsAfterDrag);

  // --- recargar: la config persiste (se relee de /api/line same-origin) ---
  await page.reload();
  await expect(page.getByTestId("line-coords")).toHaveText(savedCoords!);
  await expect(page.getByTestId("positive-side")).toHaveText(savedSide!);
  await expect(page.getByTestId("config-version")).toHaveText(savedVersion!);

  // --- incremento en vivo por WS: la fuente fake guioniza cruces ---
  const total = async () =>
    Number(await page.getByTestId("counter-in").textContent()) +
    Number(await page.getByTestId("counter-out").textContent());
  const start = await total();
  await expect.poll(total, { timeout: 15_000 }).toBeGreaterThan(start);
});
