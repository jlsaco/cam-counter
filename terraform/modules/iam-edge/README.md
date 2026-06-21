# Módulo `iam-edge` — rol + política IAM least-privilege por Pi

Provisiona un **rol IAM por dispositivo** con una **política least-privilege** acotada por
`site_id` / `device_id`. El Pi asume este rol y obtiene **credenciales STS de corta vida**;
con ellas sólo puede hacer lo justo para su operación. Cuenta `950639281773` / `us-east-1`.

Para el **primer Pi** se instancia con placeholders **no sensibles**
(`site_id = "sitio-demo"`, `device_id = "rpi-001"`); en producción se parametriza por
dispositivo (un rol/política por Pi).

---

## Permisos concedidos (EXACTOS)

| # | Servicio | Acciones | Recurso / condición | Por qué |
| --- | --- | --- | --- | --- |
| 1 | **S3 media** | `s3:PutObject`, `s3:AbortMultipartUpload`, `s3:GetObject` | `…media-…/media/${site_id}/${device_id}/*` | Subir clips SÓLO al prefijo del propio Pi. `GetObject` para reintentos idempotentes. |
| 2 | **S3 releases** | `s3:GetObject` | `…fleet-releases-…/releases/*` y `…/channels/*` | Lectura **SigV4** del agente OTA (artefactos + manifiestos). **Nunca presigned.** |
| 2b | **S3 releases** | `s3:ListBucket` | bucket releases, `Condition s3:prefix ∈ {releases/*, channels/*}` | Listar para descubrir versiones/manifiestos, acotado por prefijo. |
| 3 | **DynamoDB events** | `dynamodb:PutItem` | `cam-counter-events`, `Condition dynamodb:LeadingKeys = CAM#${site_id}#${device_id}#*` | Escribir eventos SÓLO del propio device. |
| 4 | **DynamoDB devices** | `dynamodb:GetItem`, `dynamodb:UpdateItem` | `cam-counter-devices`, `Condition dynamodb:LeadingKeys = DEVICE#${device_id}` | Heartbeat (reported_version/last_seen_at/status) SÓLO de la propia fila. NO PutItem arbitrario. |

> **Aislamiento entre dispositivos**: un Pi **no** puede escribir media (`media/otro/otro/*`),
> eventos (`CAM#otro#otro#…`) ni la fila de registro (`DEVICE#otro`) de otro dispositivo:
> ninguna acción cae bajo un `Allow` → **DENY** (demostrado con `simulate-principal-policy`,
> abajo).

### Nota sobre `dynamodb:LeadingKeys` y la PK compuesta de eventos

La PK de `cam-counter-events` es `CAM#{site_id}#{device_id}#{camera_id}` y `camera_id` varía
por cámara del mismo Pi. `dynamodb:LeadingKeys` evalúa el **valor completo** de la partition
key, por lo que se usa el operador **`ForAllValues:StringLike`** con **wildcard de sufijo**
`CAM#${site_id}#${device_id}#*`. Así se cubren todas las cámaras del device sin enumerarlas y
se bloquea cualquier otra. **Alternativa documentada** (si una cuenta/región no honrara el
wildcard en `LeadingKeys`): enumerar las cámaras del device como lista de `LeadingKeys`
(`CAM#site#device#cam1`, `…#cam2`, …) o restringir adicionalmente a nivel de aplicación. El
objetivo invariante: un device **no** escribe eventos de otro `device_id`.

---

## Trust policy — contrato estable para PR10 (F7)

El rol per-Pi DEBE poder ser asumido por:

1. **El mecanismo de provisioning del Pi en producción** — STS de **corta vida**. **IAM Roles
   Anywhere** es el hook de provisioning para **v1.1**: el Pi presentaría un **certificado
   X.509** a un *trust anchor* (ACM PCA) y un *profile* mapearía a este rol, obteniendo
   credenciales STS efímeras **sin claves de larga vida**. (No se materializa en v1.0; se
   añadirá como principal de servicio `rolesanywhere.amazonaws.com` cuando exista el anchor.)
2. **El PRINCIPAL ESTABLE del runner MAD** (`runner_principal_arn`) — para que **PR10** valide
   el IAM acotado **asumiendo este rol** y comprobando los DENY cross-device.

### Normalización del `runner_principal_arn`

`aws sts get-caller-identity` puede devolver una **sesión asumida**
(`arn:aws:sts::950639281773:assumed-role/<RoleName>/<session>`), que **NO** es un principal
válido/estable para un `Principal` de trust. Se **NORMALIZA** al **rol base**
`arn:aws:iam::950639281773:role/<RoleName>`. Si ya es un ARN de **usuario/rol IAM estable**,
se usa **tal cual**.

> En este apply el runner es el usuario IAM estable
> `arn:aws:iam::950639281773:user/raspberry`, así que se usó **tal cual** (no requirió
> normalización). El valor queda **persistido en el HCL/tfvars del root** (no en un `-var`
> efímero de CLI), de modo que un apply posterior de la pila **no rompe el trust ni recrea el
> rol** (F7 reproducible).

---

## Entrega de credenciales por dispositivo (cero secretos)

- **Producción (v1.0)**: el Pi **asume el rol** vía STS y usa credenciales de **corta vida**
  (`max_session_duration` = 1 h por defecto). **Nunca** se commiten claves de larga vida.
- **Provisioning sin claves (v1.1)**: **IAM Roles Anywhere** (certificado X.509 + trust
  anchor) entrega las credenciales STS sin secretos persistentes en el Pi.

---

## Prueba de denegación cross-device (least-privilege)

Demuestra que el rol **NO** puede escribir recursos de otro dispositivo. Se **ejecuta de
verdad** en la verificación del PR (y se documenta aquí para reproducibilidad):

```bash
ROLE_ARN=$(terraform -chdir=terraform/environments/prod output -raw edge_role_arn)

# S3: PutObject a un prefijo de OTRO sitio/device → debe salir implicitDeny.
aws iam simulate-principal-policy \
  --policy-source-arn "$ROLE_ARN" \
  --action-names s3:PutObject \
  --resource-arns 'arn:aws:s3:::cam-counter-media-950639281773/media/otro-sitio/otro-device/x.mp4'

# DynamoDB: PutItem en events con LeadingKeys de OTRO device → debe salir Deny.
aws iam simulate-principal-policy \
  --policy-source-arn "$ROLE_ARN" \
  --action-names dynamodb:PutItem \
  --resource-arns 'arn:aws:dynamodb:us-east-1:950639281773:table/cam-counter-events' \
  --context-entries 'ContextKeyName=dynamodb:LeadingKeys,ContextKeyType=stringList,ContextKeyValues=CAM#otro-sitio#otro-device#otro-device-cam0'

# DynamoDB: UpdateItem en devices de OTRA fila → debe salir Deny.
aws iam simulate-principal-policy \
  --policy-source-arn "$ROLE_ARN" \
  --action-names dynamodb:UpdateItem \
  --resource-arns 'arn:aws:dynamodb:us-east-1:950639281773:table/cam-counter-devices' \
  --context-entries 'ContextKeyName=dynamodb:LeadingKeys,ContextKeyType=stringList,ContextKeyValues=DEVICE#otro-device'
```

---

## Variables principales

| Variable | Default | Descripción |
| --- | --- | --- |
| `site_id` / `device_id` | `sitio-demo` / `rpi-001` | Slugs del Pi (placeholders no sensibles). |
| `runner_principal_arn` | — (requerido) | Principal estable que asume el rol (F7). |
| `media_bucket_arn` | — (requerido) | ARN del bucket de media. |
| `events_table_arn` | — (requerido) | ARN de la tabla de eventos. |
| `devices_table_arn` | — (requerido) | ARN de la tabla de dispositivos. |
| `releases_bucket_name` | `cam-counter-fleet-releases-950639281773` | Nombre del bucket de releases (lo crea PR11; sólo se referencia su ARN). |
| `name_prefix` | `cam-counter-edge` | Prefijo del nombre del rol/política. |
| `max_session_duration` | `3600` | Duración máxima STS (s). |
| `tags` | `{}` | Tags lógicos minúscula (F3). |

## Outputs

| Output | Descripción |
| --- | --- |
| `role_arn` | ARN REAL y resoluble del rol per-Pi (lo asume PR10). |
| `role_name` | Nombre del rol per-Pi. |
| `policy_arn` | ARN de la política managed adjunta. |

---

## Teardown

```bash
terraform -chdir=terraform/environments/prod destroy -target=module.iam_edge
```

Costo: recursos IAM de **costo cero**.
