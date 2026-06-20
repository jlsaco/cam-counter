# Módulo `events-table` — tabla DynamoDB de eventos de cruce

Crea la tabla **`cam-counter-events`**: el histórico en nube de los eventos de
cruce de línea sincronizados desde el borde. Implementa el contrato canónico
**CrossingEvent** (ver `CLAUDE.md` §8.A).

## Esquema de claves

| Clave    | Patrón                                   | Rol            | Tipo   |
| -------- | ---------------------------------------- | -------------- | ------ |
| `PK`     | `CAM#{site_id}#{device_id}#{camera_id}`  | hash (tabla)   | String |
| `SK`     | `TS#{ts_event_ms:013d}#{event_id}`       | range (tabla)  | String |
| `GSI1PK` | `SITE#{site_id}`                         | hash (GSI1)    | String |
| `GSI1SK` | `TS#{ts_event_ms:013d}#{event_id}`       | range (GSI1)   | String |

- `ts_event_ms` es epoch en **milisegundos UTC**, formateado a **13 dígitos** con
  padding de ceros (orden lexicográfico = orden temporal). `event_id` desambigua
  eventos en el mismo milisegundo.
- En DynamoDB **sólo** se declaran como `attribute` las claves de tabla y de GSI1.
  El resto del item es **schemaless**: `event_id`, `site_id`, `device_id`,
  `camera_id`, `track_id`, `direction` (`'in'|'out'`), `label`, `line_version`,
  `ts_event_ms`, `ts_event_iso`, `confidence`, `clip_key`, `clip_status`,
  `schema_version`, `created_at`.
- **Identificadores (slugs)**: `site_id`/`device_id`/`camera_id` cumplen
  `^[a-z0-9][a-z0-9-]{1,62}$`. **PROHIBIDOS `#` y `/`**: `#` delimita las claves
  compuestas de DynamoDB y `/` las rutas S3. La validación del regex se hace en el
  **borde** antes de construir la clave (no en Terraform).

## Capacidad / durabilidad

- **`PAY_PER_REQUEST`** (on-demand): sin aprovisionar capacidad.
- **Point-in-Time Recovery (PITR)**: habilitado.
- **TTL**: opcional vía `enable_ttl` (atributo `expires_at`). **DESHABILITADO por
  defecto** — el histórico no caduca salvo política explícita.

## Patrones de acceso

1. **Eventos de UNA cámara ordenados por tiempo** → `Query` sobre la tabla base por
   `PK = CAM#{site_id}#{device_id}#{camera_id}` con rango de `SK` (`begins_with` /
   `between` sobre `TS#…`).
2. **Todos los eventos de UN sitio (todas sus cámaras) ordenados por tiempo** →
   `Query` sobre **`GSI1`** por `GSI1PK = SITE#{site_id}` con rango de `GSI1SK`.
   Proyección **`ALL`** para devolver el evento completo sin segundo `GetItem`.

## Idempotencia edge→cloud (contrato A, validado en PR10)

`event_id` es **DETERMINISTA**:
`sha1(site_id|device_id|camera_id|track_id|crossing_seq)` en hex minúscula (el
`sha1` es **sólo para deduplicación**, no criptográfico). La sincronización cloud
(PR10) escribe el evento con un **conditional put** sobre la `PK`/`SK` derivadas de
ese `event_id`, de modo que reintentar el **mismo** `event_id` **no duplica**. Esta
tabla es el sustrato REAL contra el que PR10 valida ese contrato.

## Inputs

| Nombre               | Tipo          | Default               | Descripción                          |
| -------------------- | ------------- | --------------------- | ------------------------------------ |
| `table_name`         | `string`      | `cam-counter-events`  | Nombre de la tabla.                  |
| `gsi1_name`          | `string`      | `GSI1`                | Nombre del GSI1 (por sitio).         |
| `enable_ttl`         | `bool`        | `false`               | Habilita TTL.                        |
| `ttl_attribute_name` | `string`      | `expires_at`          | Atributo TTL (epoch s).              |
| `tags`               | `map(string)` | `{}`                  | Tags lógicos minúscula (F3).         |

## Outputs

| Nombre       | Descripción                                                  |
| ------------ | ----------------------------------------------------------- |
| `table_name` | Nombre de la tabla (output canónico `events_table_name`).   |
| `table_arn`  | ARN de la tabla (lo consume `iam-edge`).                    |
| `gsi1_name`  | Nombre del GSI1.                                            |

## Verificación contra AWS real

```bash
aws dynamodb describe-table             --table-name cam-counter-events
aws dynamodb describe-continuous-backups --table-name cam-counter-events  # PITR ENABLED
aws dynamodb list-tags-of-resource      --resource-arn <events_table_arn> # project / managed_by=mad-runner
```
