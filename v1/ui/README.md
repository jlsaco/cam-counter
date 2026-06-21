# `v1/ui/` — SPA local (React + TypeScript + Vite + Tailwind)

**Esqueleto; se implementa en PRs posteriores.**

SPA servida **same-origin** por la API local (`v1/api/`). Muestra el vídeo en vivo (stream
**MJPEG**) y permite dibujar la **línea de conteo** como **overlay SVG** en **coordenadas
normalizadas 0..1** relativas al frame original. Cambiar la línea **no** reinicia el
servicio: se persiste en local y el pipeline la relee vía `config_version` (hot-reload).

Pruebas E2E con **Playwright** contra la UI local.

## PR10 — harness mínimo (provisional)

Mientras PR09 (SPA React/Vite/Tailwind completa) no esté en esta base, la UI es un
**harness MÍNIMO vanilla** en `public/index.html` (overlay SVG con línea
arrastrable en coords normalizadas 0..1, botón de invertir sentido, guardar, y
contadores en vivo por WS), suficiente para una suite **Playwright E2E REAL** del
flujo: cargar → arrastrar línea → invertir → guardar → recargar (persiste) → cruce
guionizado incrementa el contador por WS.

```bash
cd v1/ui
npm ci
npx playwright install --with-deps chromium
npm run test:e2e   # levanta el harness FastAPI (CAMCOUNTER_FAKE_SOURCE=1) y corre el E2E
```

> Entorno: Playwright SÍ trae chromium-headless-shell para Linux ARM64 (corre en el
> Pi) y x86 (CI). El job `ui-e2e` de CI lo ejecuta en ubuntu-latest.
