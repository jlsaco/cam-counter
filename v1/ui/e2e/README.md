# `v1/ui/e2e/` — E2E Playwright de la UI

Paquete **separado** del de la UI a propósito: así el `npm ci` de `v1/ui` (CI de
build) **no** descarga navegadores de Playwright. La suite arranca FastAPI con la
**fuente falsa** (`CAMCOUNTER_FAKE_SOURCE=1`) sirviendo la SPA construida, sin
Pi/Hailo/cámara.

## Cobertura

- `smoke.spec.ts` — carga de la SPA same-origin, salud de producto, stream MJPEG.
- `config-line.spec.ts` — configurar la línea: **arrastrar** un extremo (overlay
  SVG client-side) → **invertir sentido** (`positive_side`) → **guardar** (PUT con
  CAS de `config_version`) → **recargar** y comprobar que **persiste** (versión
  incrementada + sentido invertido).
- `live-count.spec.ts` — un **cruce guionizado** (la fuente falsa usa
  `smooth_crossing_script`, que el tracker IoU sí sigue) recorre el pipeline REAL
  (`DummyDetector → tracker → LineCounter → Store`) y emite un `WsEnvelope`; la SPA,
  suscrita al hub WS, **incrementa el contador en vivo** (sin recargar).

La fuente falsa usa un **SQLite fresco** por corrida (`CAMCOUNTER_DB_PATH` a un
temporal) y una cadencia rápida (`CAMCOUNTER_FRAME_INTERVAL=0.05`) para aserciones
deterministas; ver `playwright.config.ts`.

## Requisitos previos

```bash
cd v1/ui && npm ci && npm run build          # genera v1/ui/dist (lo sirve FastAPI)
python -m pip install -e v1/edge -r v1/api/requirements.txt
```

## Ejecutar

```bash
# Desde v1/ui (el script delega en este paquete):
cd v1/ui
npx playwright install --with-deps chromium  # navegadores (una vez)
CAMCOUNTER_FAKE_SOURCE=1 npm run test:e2e

# O directamente desde aquí:
cd v1/ui/e2e
npm install
npx playwright install --with-deps chromium
npm test
```

El puerto es configurable con `CAMCOUNTER_PORT` (def 8000) para evitar choques en
runners ocupados.

## Nota ARM64

En algunos runners **ARM64** la descarga de navegadores
(`npx playwright install`) usa el build fallback `ubuntu*-arm64` (no oficial).
Si falla, ejecuta los E2E en un runner **x86** (lo hace CI). El resto del
build/typecheck de la UI **no** depende de Playwright.
