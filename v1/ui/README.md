# v1/ui — SPA local en la LAN (esqueleto)

Esqueleto; **se implementa en PRs posteriores**.

SPA **React + TypeScript + Vite + Tailwind** servida *same-origin* por `v1/api` desde el
Pi. Muestra el vídeo en vivo (**MJPEG**) y dibuja la **línea de conteo** como **overlay
SVG** en **coordenadas normalizadas 0..1** relativas al frame de inferencia. Permite editar
la línea y otros parámetros con **hot-reload** vía `config_version` (sin reiniciar el
servicio). Pruebas **E2E con Playwright** contra la UI local.

> Aquí solo queda el esqueleto; el código de la UI llega en PRs posteriores.
