# Módulo Terraform `fleet-releases`

Crea el **tercer** bucket S3 del producto: **artefactos OTA + manifiestos de canal**
(`cam-counter-fleet-releases-950639281773`). Es el destino de publicación de los workflows
`release.yml` / `promote.yml` y la **única fuente** de la versión deseada que lee el
update-agent (objeto `channels/<channel>/manifest.json`, vía SigV4, nunca presigned).

## Recursos

- `aws_s3_bucket.releases` — bucket privado, prefijo `cam-counter-`.
- `aws_s3_bucket_versioning` — **ON** (protege el manifiesto single-writer/If-Match).
- `aws_s3_bucket_server_side_encryption_configuration` — **SSE-S3 (AES256)**.
- `aws_s3_bucket_public_access_block` — las **4** flags en `true`.
- `aws_s3_bucket_ownership_controls` — **BucketOwnerEnforced** (ACLs deshabilitadas).
- `aws_s3_bucket_lifecycle_configuration` — **no expira contenido** (una Pi mucho tiempo
  offline debe poder resolver su versión current); sólo aborta multipart incompletos.
- `aws_s3_bucket_policy` — **TLS-only** (deny a `aws:SecureTransport=false`).

## Convención de claves

```
releases/<version>/cam-counter-edge-<version>-arm64.tar.gz
releases/<version>/cam-counter-edge-<version>-arm64.tar.gz.sha256
releases/<version>/cam-counter-edge-<version>-arm64.tar.gz.minisig
channels/<channel>/manifest.json          # canary | stable
native/<...>                              # native_blob (fuera del tarball)
```

## Modelo de despliegue (F1/F2/F3)

- Lo **aplica AUTÓNOMAMENTE el RUNNER MAD** (PR11), compartiendo el state remoto de
  `environments/prod` (lock DynamoDB de PR02). GitHub Actions CI permanece **plan-only**.
- El apply es **estrictamente ADITIVO** (F1): este módulo sólo **añade** el bucket de
  releases; el plan no debe mostrar ningún `destroy`/`replace`/`change` de PR02–PR04. Se
  aplica **sólo desde la rama apilada más alta**.
- **Tags (F3):** `default_tags` capitalizados `{Project, ManagedBy=terraform, Env}` + tags
  lógicos en MINÚSCULA `project=cam-counter` y `managed_by=mad-runner` (clave en minúscula;
  nunca `ManagedBy="mad-runner"`).

## Teardown

Bajo costo (S3; sin almacenamiento caro). Para desmontar:

```bash
# Vaciar el bucket (incluidas versiones) y destruir SÓLO este módulo.
aws s3 rm s3://cam-counter-fleet-releases-950639281773 --recursive
terraform -chdir=terraform/environments/prod destroy -target=module.fleet_releases
```

> `-target` es aceptable para teardown selectivo; para el **apply** se usa el ROOT COMPLETO
> con plan inspeccionado como aditivo (F1).
