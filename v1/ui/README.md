# `v1/ui/` — SPA local (React + TypeScript + Vite + Tailwind)

**Esqueleto; se implementa en PRs posteriores.**

SPA servida **same-origin** por la API local (`v1/api/`). Muestra el vídeo en vivo (stream
**MJPEG**) y permite dibujar la **línea de conteo** como **overlay SVG** en **coordenadas
normalizadas 0..1** relativas al frame original. Cambiar la línea **no** reinicia el
servicio: se persiste en local y el pipeline la relee vía `config_version` (hot-reload).

Pruebas E2E con **Playwright** contra la UI local (PRs posteriores).
