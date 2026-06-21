# PR10 — Supervisor multi-cámara, cloud-sync y verificación

Documenta dos cosas que NO se pueden verificar en CI x86: el **smoke EN HARDWARE**
(Hailo + cámara) y la **prueba de integración real contra AWS** (recursos de PR04).

> Sin cutover: el rollback declarado es re-habilitar `hailo-personas` (el servicio
> legacy nunca se elimina; `cam-counter-edge.service` sólo coexiste).

> Nota de la pila: en esta base PR08 (`config.py`/`clip.py`) y PR09 (FastAPI + SPA
> React + fuente fake) **aún no están presentes**. PR10 se construyó sobre lo
> realmente existente (paquete `cam_counter_edge` a nivel de PR07) adaptando rutas y
> añadiendo el mínimo necesario; ver el cuerpo del PR para el detalle de la
> discrepancia.

---

## 1. Checklist de smoke EN-PI (Hailo + cámara, NUNCA es gate de CI)

Estos chequeos requieren hardware real y se hacen a mano en el Pi:

- [ ] **Hailo presente**: `hailortcli fw-control identify` responde (acelerador
      visible) y `ls /usr/share/hailo-models/yolov8s_h8.hef` existe.
- [ ] **Presupuesto del VDevice compartido**: con ~6.6 ms de inferencia por cámara
      y el lock CORTO serializando el único Hailo VDevice, **`4 * 6.6 ms = 26.4 ms < 66 ms`**
      (margen de sobra para 15 fps por cámara). Verifícalo observando `latency_ms`
      y `fps` por cámara en `/healthz` bajo carga de 3–4 cámaras.
- [ ] **`/healthz` con `frames>0`**: `curl -s localhost:8081/healthz | jq` devuelve
      `status: "ok"` y, por cada cámara, `frames_processed > 0` y un
      `last_inference_ts` reciente. Una cámara que responde pero con
      `frames_processed == 0` aparece como `healthy: false` y el endpoint agrega
      **503 `degraded`** (distingue "vivo pero no procesa" de "sano").
- [ ] **Cruce manual**: una persona cruzando la línea incrementa el contador en
      vivo y genera un `CrossingEvent` en SQLite (`synced=0`).
- [ ] **Reinicio individual**: matar/forzar el fallo de un pipeline (p.ej. soltar
      una cámara) NO tumba a las demás; el supervisor lo reinicia (sube `restarts`).
- [ ] **Coexistencia / rollback**: `cam-counter-edge.service` se instala junto a
      `hailo-personas` SIN cutover. **Rollback = re-habilitar `hailo-personas`**
      (`systemctl disable --now cam-counter-edge && systemctl enable --now hailo-personas`).

---

## 2. Prueba de integración REAL contra AWS (recursos de PR04)

Valida el worker `cam_counter_edge/sync.py` contra el **bucket de media REAL**
(`cam-counter-media-950639281773`) y la **tabla de eventos REAL**
(`cam-counter-events`) ya desplegados por el runner MAD en PR04. Demuestra el
**contrato A** (`event_id` determinista + conditional put) contra DynamoDB REAL.

### Qué hace
1. **Asume el ROL per-Pi** de PR04 (`sts:AssumeRole` del output `edge_role_arn`) y
   opera con esas credenciales temporales — así valida que el **IAM acotado**
   permite exactamente las escrituras del borde.
2. `PutObject` real de un clip pequeño bajo el prefijo del device de selftest.
3. `PutItem` real con **conditional put** del `CrossingEvent`.
4. **Idempotencia**: reintentar el MISMO `event_id` ⇒ `ConditionalCheckFailed` ⇒
   tratado como éxito idempotente; se asERTA que NO se creó un segundo item.
5. **Read-back**: `GetItem` del evento + `HeadObject` del clip.
6. **Cleanup garantizado**: `DeleteItem` + `DeleteObject` en el teardown de la
   fixture, pase lo que pase (incluso si una aserción falla).

### Identidad de selftest e IAM acotado
El rol per-Pi de PR04 es de **mínimo privilegio y WRITE-ONLY**: `PutObject` sólo
bajo `media/sitio-demo/rpi-001/*`, `PutItem` sólo en la partición
`CAM#sitio-demo#rpi-001#*`, y `UpdateItem`/`GetItem` sólo en `DEVICE#rpi-001`. Por
eso el selftest usa la **identidad del propio device** (`site_id=sitio-demo`,
`device_id=rpi-001`, `camera_id=rpi-001-cam0`) — usar identificadores ajenos daría
`AccessDenied` bajo el rol y NO validaría el IAM acotado. El marcado de "selftest"
va en el `track_id` (con sufijo único por corrida) para aislar ejecuciones
concurrentes manteniendo el `event_id` determinista dentro de la corrida.

El **read-back** y el **cleanup** usan las credenciales del **ENTORNO** (runner),
no el rol per-Pi: el rol no tiene `GetItem` en eventos ni `Delete` en
tabla/bucket (es write-only por diseño). Eso es correcto: el borde sólo escribe.

### Cómo correrla

```bash
cd v1/edge
python3 -m pip install -e ".[dev]"   # incluye boto3

# RUNNER (con credenciales): DEBE pasar. Modo ESTRICTO (F8): un SKIP indebido
# cuenta como FALLO. El guardián de tests/conftest.py convierte
# "0 passed / N skipped" en exit-code != 0 cuando hay flag + credenciales.
CAMCOUNTER_AWS_INTEGRATION=1 AWS_REGION=us-east-1 \
  python3 -m pytest -q -m integration_aws -rs

# CI sin OIDC (sin credenciales): DEBE saltarse limpiamente (skip), nunca fallar.
python3 -m pytest -q -m integration_aws -rs
```

Fuentes de configuración (en orden de preferencia):
- ARN del rol per-Pi: `CAMCOUNTER_EDGE_ROLE_ARN`, o el output
  `terraform -chdir=terraform/environments/prod output -raw edge_role_arn` (con el
  backend remoto inicializado). **NO se reconstruye** desde placeholders.
- Recursos: `CAMCOUNTER_MEDIA_BUCKET` / `CAMCOUNTER_EVENTS_TABLE` /
  `CAMCOUNTER_DEVICES_TABLE` (defaults coherentes con §4 de la spec).
- Fallback **explícito** del orquestador (NO valida el IAM acotado, deja TODO):
  `CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS=1`. Sin él, si el ARN no se resuelve
  o el `AssumeRole` falla con la integración habilitada, la prueba **FALLA** con un
  mensaje claro atribuido a configuración de PR04 (nunca degrada en silencio).

### Limpieza / teardown
La prueba **no crea recursos persistentes**: sólo objetos/ítems de prueba que
**ELIMINA** al final. El teardown de la **infraestructura** (bucket, tablas, rol)
es responsabilidad del runner MAD vía `terraform destroy` / script de teardown —
recursos de bajo costo (S3 + DynamoDB on-demand). Verificación de sanidad opcional
tras la corrida:

```bash
aws s3 ls s3://cam-counter-media-950639281773/media/sitio-demo/rpi-001/ --recursive \
  || echo 'prefijo de selftest vacío (esperado tras cleanup)'
```
