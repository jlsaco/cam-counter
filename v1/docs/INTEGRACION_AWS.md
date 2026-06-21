# Prueba de integración REAL contra AWS (worker de cloud-sync)

Valida `cam_counter_edge.sync` (el worker de cloud-sync) contra los recursos AWS
**REALES** ya desplegados por el runner MAD en PR04 — **NO mocks**:

- bucket de media `cam-counter-media-950639281773`,
- tabla de eventos `cam-counter-events`,
- tabla de registro `cam-counter-devices`,
- rol IAM per-Pi de mínimo privilegio (`edge_role_arn` de PR04).

Demuestra el **contrato A** (`event_id` determinista + conditional put) contra
DynamoDB real: `PutObject` del clip → conditional-put del `CrossingEvent` →
**idempotencia** (reintentar el mismo `event_id` NO duplica) → read-back → CLEANUP.

La prueba vive en `v1/edge/tests/test_sync_integration_aws.py` (marker
`integration_aws`).

## Modelo de credenciales (IAM acotado)

- La **ruta de ESCRITURA** del worker (`PutObject` + `PutItem` + `UpdateItem` del
  heartbeat) usa credenciales **STS del rol per-Pi** (least-privilege): valida que
  el IAM acotado PERMITE escribir SÓLO en el propio prefijo de media
  (`media/{site}/{device}/*`) y la propia partición de DynamoDB
  (`CAM#{site}#{device}#*`, `DEVICE#{device}`).
- El **read-back** (`GetItem`/`HeadObject`/`Query`) y el **CLEANUP**
  (`DeleteItem`/`DeleteObject`) usan las credenciales del **ENTORNO** (runner): el
  rol acotado, a propósito, NO concede esas operaciones (mínimo privilegio).

**Identificadores de selftest:** deben COINCIDIR con el alcance del rol per-Pi
(por eso por defecto son `site_id=sitio-demo`, `device_id=rpi-001`, los
placeholders del PRIMER Pi en `terraform/environments/prod/main.tf`). Override por
`CAMCOUNTER_SELFTEST_SITE_ID` / `CAMCOUNTER_SELFTEST_DEVICE_ID`. Un sufijo único
en el `track_id` aísla corridas concurrentes manteniendo el `event_id`
determinista DENTRO de una corrida (para probar idempotencia).

## Fuente canónica de nombres / ARN

Se resuelven (en este orden) desde entorno → outputs de terraform → defaults:

- `edge_role_arn` (ARN del rol per-Pi): `CAMCOUNTER_EDGE_ROLE_ARN` o
  `terraform -chdir=terraform/environments/prod output -raw edge_role_arn`.
  **NO** se reconstruye desde placeholders.
- bucket/tablas: `CAMCOUNTER_MEDIA_BUCKET` / `..._EVENTS_TABLE` /
  `..._DEVICES_TABLE` o los outputs `media_bucket_name` / `events_table_name` /
  `devices_table_name`.

> Requiere el backend remoto inicializado para leer outputs:
> `terraform -chdir=terraform/environments/prod init -input=false` (read-only).
> Instala el SDK: `pip install -e 'v1/edge[sync]'` (boto3).

## Gating estricto (F8) — comando del runner

```bash
# RUNNER (con credenciales): la integración DEBE PASAR. El guardián de conftest.py
# convierte "0 passed / N skipped" en exit-code != 0 cuando el flag está activo Y
# hay credenciales resolubles (un SKIP indebido cuenta como FALLO).
cd v1/edge && CAMCOUNTER_AWS_INTEGRATION=1 python -m pytest -q -m integration_aws -rs

# CI SIN OIDC (sin credenciales): la suite se SALTA limpiamente (skip), nunca falla.
cd v1/edge && python -m pytest -q -m integration_aws -rs
```

- **Sin credenciales** → `pytest.skip(...)` (CI sin OIDC no se rompe).
- **Con `CAMCOUNTER_AWS_INTEGRATION=1` + credenciales** → un SKIP cuenta como
  FALLO; el DoD exige un PASS REAL (subió/escribió/leyó/limpió de verdad).
- Si la integración está habilitada pero **NO se resuelve el ARN del rol o el
  `AssumeRole` FALLA**, la prueba **FALLA** con mensaje claro atribuido a la config
  de PR04 (trust del rol per-Pi que no lista al principal del runner, u output
  ausente) — **nunca** degrada en silencio. Fallback documentado SÓLO si el
  orquestador lo activa con `CAMCOUNTER_AWS_INTEGRATION_ALLOW_ENV_CREDS=1` (en cuyo
  caso NO valida el IAM acotado).

## Limpieza y teardown

- La prueba **LIMPIA SIEMPRE** lo que crea (`DeleteItem` del evento +
  `DeleteObject` del clip; restauración/borrado de la fila de registro), con
  `try/finally`, aunque una aserción falle. **No** deja datos residuales en AWS.
- Verificación de sanidad opcional tras la corrida:
  ```bash
  aws s3 ls s3://cam-counter-media-950639281773/media/sitio-demo/rpi-001/ --recursive \
    || echo 'prefijo selftest vacío (esperado tras cleanup)'
  ```
- **Teardown de la INFRAESTRUCTURA** (no de los datos de prueba) es
  responsabilidad del **runner MAD** vía `terraform destroy` / script de teardown.
  Son recursos de bajo costo: **S3 + DynamoDB on-demand**. Esta prueba **NO** crea
  recursos persistentes; sólo objetos/ítems de prueba efímeros que elimina.
