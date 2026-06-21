# Módulo `events-table` — tabla DynamoDB de eventos de cruce

Provisiona **`cam-counter-events`**, el histórico en nube de los **eventos de cruce de
línea** (contrato canónico `CrossingEvent`). Cuenta `950639281773` / `us-east-1`.

`PAY_PER_REQUEST` (on-demand) · **PITR habilitado** · TTL opcional (off por defecto).

---

## Esquema de claves (EXACTO)

| Clave | Patrón | Tipo |
| --- | --- | --- |
| `PK` (hash) | `CAM#{site_id}#{device_id}#{camera_id}` | String |
| `SK` (range) | `TS#{ts_event_ms:013d}#{event_id}` | String |
| `GSI1PK` (GSI1 hash) | `SITE#{site_id}` | String |
| `GSI1SK` (GSI1 range) | `TS#{ts_event_ms:013d}#{event_id}` | String |

- `ts_event_ms` = epoch en **milisegundos UTC**, formateado a **13 dígitos** con padding de
  ceros (orden lexicográfico = orden temporal). `event_id` desambigua eventos en el mismo ms.
- `site_id` / `device_id` / `camera_id` son **slugs** `^[a-z0-9][a-z0-9-]{1,62}$`; **`#` y
  `/` PROHIBIDOS** (`#` delimita estas claves compuestas). Validación en el **edge**.
- DynamoDB es **schemaless**: en Terraform sólo se declaran como `attribute` los que
  participan en keys/índices (`PK`, `SK`, `GSI1PK`, `GSI1SK`). El resto de campos del evento
  **no** se declaran.

### GSI1 — proyección `ALL`

Las consultas por sitio devuelven el **item completo** sin un `GetItem` extra (lecturas
históricas/exportación). Trade-off aceptado: mayor coste de almacenamiento del índice a
cambio de no re-leer la tabla base.

---

## Patrones de acceso

| # | Caso de uso | Operación |
| --- | --- | --- |
| 1 | **Eventos de una cámara ordenados por tiempo** | `Query` sobre la tabla base: `PK = CAM#{site}#{device}#{camera}` + rango de `SK` (`begins_with` / `between` sobre `TS#...`). |
| 2 | **Todos los eventos de un sitio (todas sus cámaras) por tiempo** | `Query` sobre **GSI1**: `GSI1PK = SITE#{site}` + rango de `GSI1SK`. |

---

## Idempotencia de la sincronización edge→cloud (contrato A, relevante a PR10)

`event_id` es **DETERMINISTA**: `sha1(site_id|device_id|camera_id|track_id|crossing_seq)` en
hex minúscula (el `sha1` es **sólo** para deduplicación, no criptográfico). La sincronización
cloud (PR10) escribe el evento con **conditional put** sobre la PK/SK derivadas de ese
`event_id`, de modo que **reintentar el MISMO `event_id` NO duplica**. Esta tabla es el
sustrato REAL contra el que PR10 valida ese contrato.

---

## Campos del item (referencia — NO se declaran en Terraform salvo los de keys)

`event_id`, `site_id`, `device_id`, `camera_id`, `track_id`, `direction` (`'in'|'out'`),
`label`, `line_version`, `ts_event_ms`, `ts_event_iso`, `confidence`, `clip_key`,
`clip_status`, `schema_version`, `created_at`.

---

## Variables principales

| Variable | Default | Descripción |
| --- | --- | --- |
| `table_name` | `cam-counter-events` | Nombre de la tabla (prefijo `cam-counter-`). |
| `gsi1_name` | `GSI1` | Nombre del GSI por sitio. |
| `enable_ttl` | `false` | Habilita TTL nativo (hook de retención). |
| `ttl_attribute_name` | `expires_at` | Atributo TTL (epoch s) cuando `enable_ttl=true`. |
| `tags` | `{}` | Tags lógicos minúscula (F3). |

## Outputs

`table_name`, `table_arn`, `gsi1_name`.

---

## Teardown

```bash
terraform -chdir=terraform/environments/prod destroy -target=module.events_table
```

Costo: DynamoDB **on-demand** (sólo pagas por uso) + PITR; bajo costo.
