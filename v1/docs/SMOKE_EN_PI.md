# Smoke EN-PI — supervisor multi-cámara `cam-counter-edge`

Checklist de verificación **EN HARDWARE** (Raspberry Pi 5 + Hailo-8) del
supervisor multi-cámara. Estos chequeos **NO** son gate de CI (CI corre sin
hardware con `DummyDetector` + fuente falsa); se ejecutan manualmente en el Pi
tras desplegar.

> **Sin cutover.** El servicio `cam-counter-edge` COEXISTE con el legacy
> `hailo-personas`. El instalador `scripts/install_edge_service.sh` deja la unit
> DEFINIDA pero **NO habilitada** por defecto. **Rollback** = re-habilitar
> `hailo-personas`:
> ```bash
> sudo systemctl disable --now cam-counter-edge
> sudo systemctl enable  --now hailo-personas
> ```

## Presupuesto del VDevice compartido

Un **único** Hailo VDevice sirve a las N cámaras con un **lock corto** alrededor
de `infer()`. El presupuesto temporal con 4 cámaras:

```
4 cámaras × ~6.6 ms/inferencia ≈ 26.4 ms  <  66 ms (ventana de 15 fps por cámara)
```

Queda margen holgado para 3–4 cámaras a 15 fps con un solo acelerador, porque las
inferencias se **serializan** bajo el lock (nunca dos `infer()` en paralelo) y
cada una cuesta ~6.6 ms.

## Arranque manual

```bash
# Render + (opcional) activación deliberada de la unit:
scripts/install_edge_service.sh                 # define la unit, NO la habilita
ENABLE_NOW=1 scripts/install_edge_service.sh    # CUTOVER deliberado (cuando proceda)

# O ejecución directa para diagnóstico (sin systemd):
CAMCOUNTER_SITE_ID=sitio-demo CAMCOUNTER_DEVICE_ID=rpi-001 \
CAMCOUNTER_CAMERA_COUNT=3 CAMCOUNTER_HEALTHZ_PORT=8081 \
CAMCOUNTER_RTSP_URL='rtsp://...' \
  python -m cam_counter_edge.app
```

## Checklist

- [ ] **Hailo presente**: `hailortcli fw-control identify` responde (acelerador
      detectado) y `python -c "import hailo_platform"` no falla en el Pi.
- [ ] **`/healthz` responde**: `curl -s localhost:8081/healthz | jq` devuelve
      `status` y un array `cameras`.
- [ ] **`frames_processed > 0` por cámara**: cada cámara en `/healthz` muestra
      `frames_processed` CRECIENTE y `last_inference_ts` RECIENTE. Una cámara que
      responde pero con `frames_processed == 0` se reporta **`degraded` / HTTP
      503** (distinguible de salud real).
- [ ] **`fps`/`latency_ms`/`hailo_busy`** razonables por cámara (fps ≈ cadencia de
      la cámara; `latency_ms` ~ ms por inferencia).
- [ ] **Cruce manual**: una persona cruza la línea configurada → el contador del
      sentido correcto incrementa (verlo en la UI local y/o en SQLite).
- [ ] **Reinicio individual**: matar/forzar el fallo de UNA cámara (p.ej. cortar su
      RTSP) NO tumba a las demás; el supervisor **reinicia ese pipeline** solo
      (`restarts` en `/healthz` sube para esa cámara).
- [ ] **Coexistencia**: `hailo-personas` sigue ejecutable; `rtsp-enable/` intacto.

## Sync edge → cloud (opcional, en el Pi)

El worker de cloud-sync (`cam_counter_edge.sync`) drena los eventos locales
`synced=0` a la nube de forma **idempotente** y **tolerante a offline** (el conteo
y la persistencia local NUNCA dependen de la red). Para validarlo contra AWS REAL,
ver [`INTEGRACION_AWS.md`](./INTEGRACION_AWS.md).
