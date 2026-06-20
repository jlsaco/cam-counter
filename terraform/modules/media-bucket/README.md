# Módulo `media-bucket` — bucket S3 de media del producto

Crea el bucket **`cam-counter-media-950639281773`**, donde el borde (Pi) sube los
**recortes/clips, gifs y snapshots** de cada evento de cruce.

Es uno de los **tres buckets jamás conflados** (ver `CLAUDE.md` §7). **NO** es el
bucket de artifacts de ops (`cam-counter-rpi-artifacts-…`, reservado/no tocar) ni
el de releases OTA (`cam-counter-fleet-releases-…`, creado en PR11).

## Seguridad (igual que todos los buckets NUEVOS de la iniciativa)

- **Privado** + **Block Public Access** con las 4 flags en `true`.
- **Cifrado en reposo SSE-S3 (AES256)** (`bucket_key_enabled` para abaratar KMS-less).
- **Object Ownership `BucketOwnerEnforced`** (ACLs deshabilitadas).
- **Bucket policy TLS-only**: `Deny` a cualquier petición con
  `aws:SecureTransport = false` (obliga HTTPS).
- **Versionado**: `Enabled` por defecto (recomendado), para recuperar clips
  sobrescritos o borrados accidentalmente.

## Ciclo de vida

| Acción                                   | Cuándo            | Variable               |
| ---------------------------------------- | ----------------- | ---------------------- |
| Transición a `STANDARD_IA`               | a los **30 días** | `transition_ia_days`   |
| Expiración (borrado) del objeto          | a los **365 días**| `expiration_days`      |
| Abort de subidas multipart incompletas   | a los **7 días**  | `abort_multipart_days` |

## Convención de claves (patrón de acceso)

```
media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.{ext}
```

- `site_id` / `device_id` / `camera_id` son **slugs** `^[a-z0-9][a-z0-9-]{1,62}$`
  (sin `#` ni `/`); el regex se valida en el **borde** antes de construir la clave.
- El Pi sólo puede escribir bajo **su** prefijo `media/{site_id}/{device_id}/*`
  (least-privilege en el módulo `iam-edge`).
- **NOTA cross-PR (PR10)**: la prueba de integración real subirá un clip a
  `media/_selftest/...` y lo borrará; este bucket y la política IAM son coherentes
  con ese flujo.

## Inputs

| Nombre                 | Tipo          | Default                            | Descripción                          |
| ---------------------- | ------------- | ---------------------------------- | ------------------------------------ |
| `bucket_name`          | `string`      | `cam-counter-media-950639281773`   | Nombre del bucket.                   |
| `enable_versioning`    | `bool`        | `true`                             | Versionado del bucket.               |
| `transition_ia_days`   | `number`      | `30`                               | Días → `STANDARD_IA`.                |
| `expiration_days`      | `number`      | `365`                              | Días → expiración.                   |
| `abort_multipart_days` | `number`      | `7`                                | Días → abort multipart incompleto.   |
| `tags`                 | `map(string)` | `{}`                               | Tags lógicos minúscula (F3).         |

## Outputs

| Nombre        | Descripción                                              |
| ------------- | ------------------------------------------------------- |
| `bucket_name` | Nombre del bucket (output canónico `media_bucket_name`).|
| `bucket_arn`  | ARN del bucket (lo consume `iam-edge`).                 |

## Verificación contra AWS real

```bash
aws s3api head-bucket          --bucket cam-counter-media-950639281773
aws s3api get-bucket-encryption --bucket cam-counter-media-950639281773
aws s3api get-public-access-block --bucket cam-counter-media-950639281773
aws s3api get-bucket-policy     --bucket cam-counter-media-950639281773   # deny TLS-only
aws s3api get-bucket-tagging    --bucket cam-counter-media-950639281773   # project=cam-counter, managed_by=mad-runner
```
