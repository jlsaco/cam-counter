# `v1/ui/` — SPA local (React + TypeScript + Vite + Tailwind)

SPA servida **same-origin** por la API local (`v1/api/`). Muestra el **vídeo en
vivo** (stream MJPEG), permite **editar la línea de conteo** como overlay SVG en
**coordenadas normalizadas 0..1**, ver **contadores** en vivo (WebSocket) e
**histórico**. Cambiar la línea **no reinicia** el servicio: se persiste en local y
el pipeline la relee vía `config_version` (hot-reload).

## Estructura

```
src/
  api/
    types.ts     # tipos TS coherentes con v1/api/schemas.py (y contracts/)
    client.ts    # cliente API tipado (fetch same-origin) + ConfigConflictError (409)
    ws.ts        # cliente WebSocket con RECONEXIÓN (backoff acotado)
  components/
    LiveView.tsx      # <img> MJPEG + LineOverlay + contadores en vivo
    LineOverlay.tsx   # SVG: coords normalizadas -> tamaño RENDERIZADO del <img>
    LineEditor.tsx    # arrastrar extremos (sin round-trip) + invertir sentido + guardar (maneja 409)
    CameraSwitcher.tsx
    HistoryTable.tsx  # histórico paginado de CrossingEvent
  hooks/useElementSize.ts   # ResizeObserver para el mapeo normalizado->px
  App.tsx, main.tsx, index.css
```

## Conceptos

- **Coordenadas normalizadas 0..1**: la línea se guarda y edita en 0..1 relativo al
  frame original. `LineOverlay` mapea esas coords al **tamaño renderizado** del
  `<img>` (medido con `ResizeObserver`); el píxel sólo existe client-side.
- **Drag sin round-trip**: arrastrar un extremo actualiza el estado local a ~60fps;
  sólo "Guardar" hace `PUT`.
- **Invertir sentido**: el toggle mapea a `positive_side` (`+1`/`-1`).
- **Manejo de 409**: si el `config_version` quedó stale, el cliente recarga la
  config vigente y deja reintentar.
- **Same-origin**: todas las llamadas son a `/api/*` del mismo origen (sin CORS).
  El gate opcional de token se envía por `X-API-Token` si hay uno en `localStorage`
  (`camcounter_api_token`).

## Build y desarrollo

```bash
cd v1/ui
npm ci                # requiere package-lock.json (commiteado)
npm run build         # tsc --noEmit && vite build  ->  dist/  (lo sirve FastAPI)
npx tsc --noEmit      # sólo typecheck
npm run dev           # dev server Vite (proxy /api -> http://127.0.0.1:8000, ws incluido)
```

`dist/` está **gitignored**: lo genera el CI (job `ui`) y, en el Pi, el instalador
antes de arrancar la API; FastAPI lo monta same-origin con fallback a `index.html`.

## E2E (Playwright)

La app es **E2E-able** con `CAMCOUNTER_FAKE_SOURCE=1` (fuente determinista: vídeo +
cruces reproducibles sin Pi/Hailo/cámara). El scaffold de Playwright vive en
`e2e/` (config + smoke test). La **suite completa llega en PR10**; aquí se deja
lista la base.

> Nota ARM64: en algunos runners ARM64 la descarga de navegadores de Playwright
> (`npx playwright install`) puede no estar disponible. En ese caso, ejecuta los
> E2E en un runner x86 (lo hará PR10) — el resto del build/typecheck de la UI no
> depende de Playwright.
