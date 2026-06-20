# Módulo `media-bucket` — bucket S3 de media del producto

Provisiona el bucket **`cam-counter-media-950639281773`** donde el Pi sube los **clips /
gifs / snapshots** de cada evento de cruce. Cuenta `950639281773` / `us-east-1`, prefijo
`cam-counter-`.

> Es uno de los **TRES** buckets distintos del producto, **jamás conflados**:
> | Bucket | Para qué | Quién lo crea |
> | --- | --- | --- |
> | `cam-counter-rpi-artifacts-950639281773` | Backup de binarios de ops (**EXISTENTE**) | — **RESERVADO / NO TOCAR** |
> | `cam-counter-media-950639281773` | **Media del producto** (clips/gifs/snapshots) | **este módulo (PR04)** |
> | `cam-counter-fleet-releases-950639281773` | Artefactos OTA + manifiestos de canal | PR11 |

---

## Convención de claves (key layout)

```
media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.{ext}
```

- `site_id` / `device_id` / `camera_id` son **slugs ASCII minúscula** que cumplen
  `^[a-z0-9][a-z0-9-]{1,62}$`. Los caracteres `#` y `/` están **PROHIBIDOS** en los slugs
  (`/` delimita rutas S3; `#` delimita claves compuestas DynamoDB). La validación del regex
  se hace en el **edge** antes de construir la clave.
- La política IAM por-Pi (`iam-edge`) restringe `s3:PutObject` al prefijo
  `media/${site_id}/${device_id}/*` del bucket, de modo que un dispositivo **no** puede
  escribir media de otro.
- **NOTA cross-PR**: PR10 hace una prueba de integración real que sube un clip bajo un
  prefijo de autotest marcado (`media/_selftest/...`) y luego lo borra.

---

## Configuración de seguridad (bucket NUEVO)

| Aspecto | Valor | Recurso |
| --- | --- | --- |
| Acceso público | **Bloqueado** (4 flags en `true`) | `aws_s3_bucket_public_access_block` |
| Cifrado en reposo | **SSE-S3 (AES256)** + bucket key | `aws_s3_bucket_server_side_encryption_configuration` |
| Object Ownership | **BucketOwnerEnforced** (ACLs deshabilitadas) | `aws_s3_bucket_ownership_controls` |
| TLS-only | **Deny** a `aws:SecureTransport=false` | `aws_s3_bucket_policy` |
| Versionado | **Enabled** (recomendado) | `aws_s3_bucket_versioning` |

### Lifecycle

| Regla | Valor (default) | Por qué |
| --- | --- | --- |
| Transición a `STANDARD_IA` | **30 días** | media fría más barata |
| Expiración (borrado) | **365 días** | retención del histórico de clips |
| Abort multipart incompletos | **7 días** | limpieza de subidas a medias |

---

## Variables principales

| Variable | Default | Descripción |
| --- | --- | --- |
| `bucket_name` | `cam-counter-media-950639281773` | Nombre del bucket (prefijo `cam-counter-`). |
| `enable_versioning` | `true` | Versionado del bucket. |
| `transition_ia_days` | `30` | Días → `STANDARD_IA`. |
| `expiration_days` | `365` | Días → expiración. |
| `abort_multipart_days` | `7` | Días → abort multipart incompletos. |
| `tags` | `{}` | Tags lógicos minúscula (F3) mergeados en el bucket. |

## Outputs

| Output | Descripción |
| --- | --- |
| `bucket_name` | Nombre del bucket de media. |
| `bucket_arn` | ARN del bucket de media. |

---

## Teardown

```bash
# Vaciar el bucket primero si tiene objetos (p.ej. media/_selftest de PR10):
#   aws s3 rm s3://cam-counter-media-950639281773 --recursive
terraform -chdir=terraform/environments/prod destroy -target=module.media_bucket
```

Costo: almacenamiento S3 bajo demanda (bajo costo; el lifecycle abarata/expira la media).
