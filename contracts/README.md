# contracts — JSON Schemas canónicos

Aquí viven los **JSON Schemas canónicos** (draft 2020-12) del producto `cam-counter`. Son
el **espejo-contrato** de los modelos **Pydantic** ejecutables que llegan en PRs
posteriores: documentan los campos exactos (snake_case), tipos, enums y patrones que fluyen
entre edge, API/UI, DynamoDB y OTA.

Schemas:

| Archivo | Qué describe |
|---|---|
| `crossing_event.schema.json` | Evento de cruce de línea (SQLite local + DynamoDB de eventos). |
| `line_config.schema.json` | Config de la línea-umbral por cámara (hot-reload por `config_version`). |
| `device_registry_item.schema.json` | Item del registro de dispositivos (DynamoDB `cam-counter-devices`). |
| `channel_manifest.schema.json` | Manifiesto por canal en el bucket de releases OTA. |
| `bundle_manifest.schema.json` | Manifiesto embebido en el artefacto OTA. |

## Compatibilidad

Cualquier **rename o cambio incompatible de un campo es BREAKING** → requiere **bump de
`schema_version`** en el schema afectado (y la correspondiente migración en los lectores).
Añadir campos opcionales es compatible.

## Convenciones transversales

- Identificadores `site_id`/`device_id`/`camera_id`: slugs ASCII minúscula
  `^[a-z0-9][a-z0-9-]{1,62}$` (sin `#` ni `/`). `camera_id` global único = `{device_id}-cam{N}`.
- Geometría: floats **normalizados 0..1** relativos al frame original (origen arriba-izquierda),
  **nunca píxeles**.
- `event_id` **determinista** = sha1 hex minúscula de
  `site_id|device_id|camera_id|track_id|crossing_seq` (sha1 **no** criptográfico, solo
  dedupe idempotente del sync).
- Versionado SemVer (`vX.Y.Z`, prereleases `-rc.N`): un único string que fluye por
  bundle-manifest, channel-manifest, device-registry y `/api/device`.
