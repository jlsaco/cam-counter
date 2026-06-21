# `v1/ui/e2e/` — E2E Playwright (scaffold)

Paquete **separado** del de la UI a propósito: así el `npm ci` de `v1/ui` (CI de
build) **no** descarga navegadores de Playwright. La **suite completa llega en
PR10**; aquí queda la base lista y la app E2E-able vía `CAMCOUNTER_FAKE_SOURCE=1`.

## Requisitos previos

```bash
cd v1/ui && npm ci && npm run build          # genera v1/ui/dist (lo sirve FastAPI)
python -m pip install -e v1/edge -r v1/api/requirements.txt
```

## Ejecutar

```bash
cd v1/ui/e2e
npm install
npm run install:browsers     # npx playwright install --with-deps chromium
npm test                     # arranca Uvicorn (fuente falsa) y corre el smoke
```

## Nota ARM64

En algunos runners **ARM64** la descarga de navegadores
(`npx playwright install`) puede no estar disponible. Si falla, ejecuta los E2E en
un runner **x86** (lo hará PR10). El resto del build/typecheck de la UI **no**
depende de Playwright.
