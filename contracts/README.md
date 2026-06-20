# `contracts/` — JSON Schemas canónicos

Aquí viven los **JSON Schemas canónicos** (JSON Schema **draft 2020-12**) que son el
**espejo documental** de los modelos Pydantic ejecutables que llegarán en PRs posteriores.
En este punto son **documentación-contrato**: definen los nombres de campo (snake_case),
tipos, enums y patrones acordados del producto.

## Schemas

| Archivo | Qué describe |
|---|---|
| `crossing_event.schema.json` | Evento de **cruce de línea** (snake_case, `schema_version=1`). |
| `line_config.schema.json` | Config de la **línea-umbral** por cámara (hot-reload vía `config_version`). |
| `device_registry_item.schema.json` | Item del **device-registry** (DynamoDB `cam-counter-devices`). |
| `channel_manifest.schema.json` | **Manifiesto por canal** en el bucket de releases (`channels/<channel>/manifest.json`). |
| `bundle_manifest.schema.json` | **Manifiesto embebido** en el artefacto OTA. |

## Convenciones transversales
- Identificadores `site_id` / `device_id` / `camera_id`: slugs ASCII en minúscula con
  patrón **`^[a-z0-9][a-z0-9-]{1,62}$`** (sin `#`, sin `/`). `camera_id` global único con
  forma `{device_id}-cam{N}`.
- Geometría: floats normalizados **0..1** relativos al frame original, origen arriba-izquierda.
  Nunca píxeles.
- `event_id` **determinista** = `sha1` hex-minúscula de
  `site_id|device_id|camera_id|track_id|crossing_seq` (sha1 **no** criptográfico, sólo
  dedupe idempotente del sync).
- Versionado: **SemVer** canónico (`vX.Y.Z`, prereleases `-rc.N`); un único string fluye por
  bundle-manifest, channel-manifest, device-registry y `/api/device`.

## Compatibilidad
Cualquier **rename de campo es BREAKING** → requiere **bump de `schema_version`**. Mantener
estos schemas coherentes con los futuros modelos Pydantic es responsabilidad de cada PR que
toque el contrato.
