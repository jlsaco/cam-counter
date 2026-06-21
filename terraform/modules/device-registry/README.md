# Módulo `device-registry` — tabla DynamoDB de registro de dispositivos

Provisiona **`cam-counter-devices`**, el **registro de la flota** (contrato canónico
`Device registry`). Cuenta `950639281773` / `us-east-1`.

`PAY_PER_REQUEST` (on-demand) · **PITR habilitado**.

---

## Esquema de claves (EXACTO)

| Clave | Patrón | Tipo |
| --- | --- | --- |
| `PK` (hash) | `DEVICE#{device_id}` | String |
| `GSI1PK` (GSI1 hash) | `CHANNEL#{release_channel}` (`canary` \| `stable`) | String |
| `GSI1SK` (GSI1 range) | `DEVICE#{device_id}` | String |

- `device_id` es un **slug** `^[a-z0-9][a-z0-9-]{1,62}$`; **`#` y `/` PROHIBIDOS**.
- DynamoDB es **schemaless**: en Terraform sólo se declaran como `attribute` los de
  keys/índices (`PK`, `GSI1PK`, `GSI1SK`). El resto del item **no** se declara.
- GSI1 con proyección **`ALL`**: el orquestador de release lee el estado completo de cada
  dispositivo del canal sin un `GetItem` extra por device.

---

## ⚠️ `desired_version` es un ESPEJO, NO la fuente de verdad

> **El `update-agent` del Pi NUNCA lee `desired_version` de esta tabla para decidir qué
> actualizar.** La **única** fuente de la versión deseada es el **manifiesto del canal en
> S3** (`channels/<channel>/manifest.json`), leído por el agente vía **SigV4** (nunca URLs
> presigned), sobre el bucket `cam-counter-fleet-releases-950639281773`.

- `desired_version` lo **escribe la nube** (workflows de release/promote) como **espejo /
  observabilidad** de lo que el manifiesto del canal indica; sirve para dashboards y
  auditoría, no para que el agente decida.
- La tabla recibe del Pi sólo **heartbeat**: `UpdateItem` de `reported_version`,
  `last_seen_at`, `status` (y campos de progreso del update). El agente nunca hace `GetItem`
  de esta tabla para descubrir la versión deseada.

Esto evita una fuente de verdad doble: el manifiesto S3 (autoritativo, firmado) manda; la
tabla observa.

---

## Patrones de acceso

| # | Caso de uso | Operación |
| --- | --- | --- |
| 1 | **Estado de un dispositivo** | `GetItem` por `PK = DEVICE#{device_id}`. |
| 2 | **Enumerar todos los dispositivos de un canal** (canary→flota) | `Query` sobre **GSI1**: `GSI1PK = CHANNEL#{release_channel}`. |

---

## Esquema de item FUSIONADO (referencia — NO se declara en Terraform salvo los de keys)

| Campo | Quién escribe | Notas |
| --- | --- | --- |
| `device_id` | provisioning | clave (`DEVICE#`) |
| `site_id` | provisioning | |
| `camera_ids` | provisioning | lista; `{device_id}-cam{N}` |
| `release_channel` | nube | `'canary'` \| `'stable'` (clave GSI1) |
| `desired_version` | **nube (ESPEJO)** | **NO** es fuente de verdad del agente |
| `reported_version` | **Pi (heartbeat)** | versión activa reportada |
| `last_good_version` | Pi | última versión sana (rollback) |
| `last_update_status` | Pi | `idle`/`downloading`/`verifying`/`activating`/`healthy`/`rolled_back`/`failed` |
| `last_update_error` | Pi | mensaje de error del último update |
| `last_seen_at` | Pi (heartbeat) | ISO-8601 UTC |
| `agent_version` | Pi | versión del update-agent |
| `status` | Pi | `online`/`offline`/`updating`/`degraded` |
| `hardware` | provisioning | `{model, hailo_fw}` |

---

## Variables principales

| Variable | Default | Descripción |
| --- | --- | --- |
| `table_name` | `cam-counter-devices` | Nombre de la tabla (prefijo `cam-counter-`). |
| `gsi1_name` | `GSI1` | Nombre del GSI por canal. |
| `tags` | `{}` | Tags lógicos minúscula (F3). |

## Outputs

`table_name`, `table_arn`, `gsi1_name`.

---

## Teardown

```bash
terraform -chdir=terraform/environments/prod destroy -target=module.device_registry
```

Costo: DynamoDB **on-demand** + PITR; bajo costo.
