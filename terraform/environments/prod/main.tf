# Raíz live del ÚNICO entorno de producción de la pila de infra.
#
# Aquí se instancian los módulos de `terraform/modules/` y vive el ÚNICO state de
# producción compartido por toda la pila apilada (PR02→PR03→PR04→…→PR11), con backend
# S3 + lock DynamoDB (ver backend.tf / backend.tf.example).
#
# F1 — State aditivo y monótono: el runner sólo aplica desde la rama apilada MÁS ALTA
# con todo el HCL acumulado; nunca se reaplica una rama inferior tras una superior.
# F2 — Apply autónomo acotado: los módulos enumerados de la pila son `state-backend` (PR02),
# `iam-github-oidc` (PR03) y `media-bucket` / `events-table` / `device-registry` / `iam-edge`
# (PR04).

locals {
  # Bucket de releases OTA: lo CREA PR11. Aquí sólo se referencia su NOMBRE para que la
  # política IAM per-Pi (módulo iam-edge) pueda construir su ARN y conceder lectura SigV4
  # (channels/* + releases/*). Referenciar el ARN NO requiere que el bucket exista todavía.
  releases_bucket_name = "cam-counter-fleet-releases-950639281773"

  # Principal ESTABLE del runner MAD que asume el rol per-Pi (F7), persistido en HCL (no en
  # un `-var` efímero) para que un apply posterior de la pila no rompa el trust ni recree el
  # rol. `aws sts get-caller-identity` devolvió el usuario IAM ESTABLE
  # `arn:aws:iam::950639281773:user/raspberry` (no una sesión assumed-role), así que se usa
  # tal cual (no requirió normalización assumed-role→role base). PR10 lo usa para asumir el
  # rol per-Pi y validar el least-privilege.
  runner_principal_arn = "arn:aws:iam::950639281773:user/raspberry"

  # Placeholders NO sensibles del PRIMER Pi (en producción se parametriza por dispositivo).
  edge_site_id   = "sitio-demo"
  edge_device_id = "rpi-001"
}

module "state_backend" {
  source = "../../modules/state-backend"
  # Sin overrides: se usan los defaults del módulo (nombres reales del producto).
}

# PR03 — Proveedor OIDC de GitHub Actions + DOS roles SEPARADOS (plan/deploy).
# El rol PLAN (solo lectura) lo asume el CI plan-only vía OIDC; el rol DEPLOY queda creado
# para operación futura (release/promote). El `apply` de esta pila lo hace el RUNNER MAD.
# Las políticas del rol PLAN se acotan al state real referenciando el módulo state-backend.
# F3 — tags lógicos minúscula `project`/`managed_by = "mad-runner"` (además de default_tags).
module "iam_github_oidc" {
  source = "../../modules/iam-github-oidc"

  # AWS IAM trata las claves de tag como CASE-INSENSITIVE: el esquema F3 dual-case del
  # proveedor por defecto haría fallar `CreateRole` (Project/project, ManagedBy/managed_by).
  # Por eso este módulo recibe el proveedor IAM-safe `aws.iam` como su `aws` por defecto; sus
  # `default_tags` { Env, project=cam-counter, managed_by=mad-runner } cumplen F3 (clave
  # minúscula) sin colisión. El módulo state_backend (S3/DynamoDB, case-sensitive) sigue en el
  # proveedor por defecto con F3 dual-case completo.
  providers = {
    aws = aws.iam
  }

  tfstate_bucket_name     = module.state_backend.state_bucket_name
  tfstate_lock_table_name = module.state_backend.lock_table_name

  tags = {
    project    = "cam-counter"
    managed_by = "mad-runner"
  }
}

# ═══════════════════════════════════ PR04 — datos del producto ═══════════════════════════════════
#
# Cuatro módulos nuevos que forman el contrato cross-subsistema (edge, cloud-sync, OTA, CI):
# media-bucket + events-table + device-registry + iam-edge. Todos usan el state remoto
# compartido `environments/prod`. El apply lo hace el RUNNER MAD (autónomo); CI sigue plan-only.
#
# F3 — TAGS: media-bucket / events-table / device-registry usan el proveedor por defecto (S3 y
# DynamoDB distinguen mayúsculas, así que su default_tags dual-case es válido). El módulo
# iam-edge crea SÓLO recursos IAM (claves de tag CASE-INSENSITIVE) y por eso recibe el
# proveedor IAM-safe `aws.iam` (igual que iam_github_oidc), evitando «Duplicate tag keys».

# PR04 (1/4) — Bucket S3 de media del producto (clips/gifs/snapshots).
module "media_bucket" {
  source = "../../modules/media-bucket"
  # Sin overrides: nombre real del producto y lifecycle/seguridad por defecto.
}

# PR04 (2/4) — Tabla DynamoDB de eventos de cruce (histórico en nube).
module "events_table" {
  source = "../../modules/events-table"
  # Sin overrides: PK/SK/GSI1, PAY_PER_REQUEST, PITR on, TTL off por defecto.
}

# PR04 (3/4) — Tabla DynamoDB de registro de dispositivos (espejo de observabilidad).
module "device_registry" {
  source = "../../modules/device-registry"
  # Sin overrides: PK/GSI1, PAY_PER_REQUEST, PITR on.
}

# PR04 (4/4) — Rol + política IAM least-privilege del PRIMER Pi.
# Usa el proveedor IAM-safe `aws.iam` (claves de tag case-insensitive en IAM). Consume los
# ARNs de los otros tres módulos + el nombre del bucket de releases (local) + el
# runner_principal_arn estable (F7, persistido en HCL).
module "iam_edge" {
  source = "../../modules/iam-edge"

  providers = {
    aws = aws.iam
  }

  site_id              = local.edge_site_id
  device_id            = local.edge_device_id
  runner_principal_arn = local.runner_principal_arn

  media_bucket_arn     = module.media_bucket.bucket_arn
  events_table_arn     = module.events_table.table_arn
  devices_table_arn    = module.device_registry.table_arn
  releases_bucket_name = local.releases_bucket_name

  tags = {
    project    = "cam-counter"
    managed_by = "mad-runner"
  }
}

# ═══════════════════════════════════ PR11 — bucket de releases OTA ═══════════════════════════════════
#
# Tercer bucket del producto: artefactos OTA + manifiestos de canal. Lo APLICA AUTÓNOMAMENTE
# el RUNNER MAD en PR11 (F2), compartiendo este state remoto compartido. Apply ESTRICTAMENTE
# ADITIVO (F1): sólo AÑADE este bucket; no toca recursos de PR02–PR04. Usa el proveedor por
# defecto (S3 distingue mayúsculas; el esquema F3 dual-case es válido). El nombre se reusa de
# `local.releases_bucket_name`, el MISMO que iam_edge usa para construir el ARN de lectura
# SigV4 del agente (channels/* + releases/*), garantizando que política y bucket coinciden.
module "fleet_releases" {
  source = "../../modules/fleet-releases"

  bucket_name = local.releases_bucket_name

  tags = {
    project    = "cam-counter"
    managed_by = "mad-runner"
  }
}
