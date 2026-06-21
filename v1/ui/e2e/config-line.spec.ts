import { expect, test, type Page } from "@playwright/test";

// E2E: configurar la línea de conteo desde la UI y verificar que PERSISTE.
// Flujo: cargar -> arrastrar un extremo de la línea (overlay SVG, client-side) ->
// invertir el sentido (positive_side) -> guardar (PUT con CAS de config_version) ->
// recargar -> la config persiste (sentido invertido + config_version incrementado).

const CAM = "demo-pi-cam0";

function sideFromButtonText(text: string): 1 | -1 {
  return text.includes("positive_side=1") ? 1 : -1;
}

async function readSide(page: Page): Promise<1 | -1> {
  const btn = page.getByRole("button", { name: /Invertir sentido/ });
  await expect(btn).toBeVisible();
  return sideFromButtonText((await btn.textContent()) ?? "");
}

async function readBaseVersion(page: Page): Promise<number> {
  const txt = (await page.getByText(/base v\d+/).textContent()) ?? "";
  return Number(txt.match(/base v(\d+)/)?.[1] ?? "0");
}

test("configurar la línea: arrastrar + invertir sentido + guardar persiste tras recarga", async ({
  page,
}) => {
  await page.goto("/");
  await page.getByRole("button", { name: new RegExp(CAM) }).click();
  await expect(page.getByRole("heading", { name: "Editar línea" })).toBeVisible();

  const initialSide = await readSide(page);
  const initialVersion = await readBaseVersion(page);

  // Coordenadas A iniciales (texto "A=(x,y) B=(x,y)").
  const coords = page.getByText(/A=\(/);
  await expect(coords).toBeVisible();
  const beforeCoords = (await coords.textContent()) ?? "";

  // Arrastrar el extremo A de la línea (drag client-side, sin round-trip).
  const handleA = page.locator('[aria-label="endpoint-a"]');
  await expect(handleA).toBeVisible();
  const box = await handleA.boundingBox();
  expect(box).not.toBeNull();
  if (box) {
    await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
    await page.mouse.down();
    await page.mouse.move(box.x - 60, box.y + 40, { steps: 10 });
    await page.mouse.up();
  }
  // El texto de coordenadas refleja el arrastre (cambió respecto al inicial).
  await expect(coords).not.toHaveText(beforeCoords);

  // Invertir el sentido (positive_side flip) y guardar (PUT con CAS).
  await page.getByRole("button", { name: /Invertir sentido/ }).click();
  await page.getByRole("button", { name: "Guardar" }).click();

  // El guardado aterrizó: la versión base (config_version) se incrementó.
  await expect
    .poll(() => readBaseVersion(page), { timeout: 10_000, intervals: [200] })
    .toBeGreaterThan(initialVersion);

  // Recargar: la config persiste (servida desde SQLite por la API).
  await page.reload();
  await page.getByRole("button", { name: new RegExp(CAM) }).click();
  await expect(page.getByRole("heading", { name: "Editar línea" })).toBeVisible();

  // El sentido quedó INVERTIDO respecto al inicial y la versión persistió > inicial.
  expect(await readSide(page)).toBe(initialSide === 1 ? -1 : 1);
  expect(await readBaseVersion(page)).toBeGreaterThan(initialVersion);
});
