# Raíz live del ÚNICO entorno de producción de la pila de infra.
#
# Aquí se instancian los módulos de `terraform/modules/` y vive el ÚNICO state de
# producción compartido por toda la pila apilada (PR02→PR03→PR04→…→PR11), con backend
# S3 + lock DynamoDB (ver backend.tf / backend.tf.example).
#
# F1 — State aditivo y monótono: el runner sólo aplica desde la rama apilada MÁS ALTA
# con todo el HCL acumulado; nunca se reaplica una rama inferior tras una superior.
# F2 — Apply autónomo acotado: los módulos enumerados de la pila son `state-backend` (PR02)
# y `iam-github-oidc` (PR03).

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
