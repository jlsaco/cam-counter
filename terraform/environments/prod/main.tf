# ─────────────────────────────────────────────────────────────────────────────
# Composición raíz del entorno `prod` (único entorno del producto).
#
# Mantiene el ÚNICO state de producción, ADITIVO Y MONÓTONO (F1), compartido por
# TODA la pila de PRs de infra. En PR02 sólo se instancia el backend de estado
# (bucket de tfstate + tabla de lock). Los PRs posteriores AÑADIRÁN módulos a este
# mismo state SIN destruir lo previo:
#   PR03 → provider OIDC + roles IAM (plan/deploy)
#   PR04 → bucket de media + tablas eventos/devices + IAM per-Pi
#   …
#   PR11 → bucket de releases OTA
#
# El runner MAD aplica SÓLO desde la rama apilada MÁS ALTA con todo el HCL
# acumulado; NUNCA reaplica esta rama (la más baja) una vez que un PR superior
# haya aplicado contra este mismo state. Ver README.md (F1).
# ─────────────────────────────────────────────────────────────────────────────

module "state_backend" {
  source = "../../modules/state-backend"
}

# ─────────────────────────────────────────────────────────────────────────────
# PR03 — Proveedor OIDC de GitHub Actions + DOS roles IAM SEPARADOS (plan/deploy).
#
# CI asume el rol `plan` (SOLO LECTURA) vía OIDC; el `apply` de infra lo hace el
# RUNNER MAD con las credenciales de su entorno (F2). El rol `deploy` queda creado
# para operación futura (release/promote → OBJETOS S3, que NO es apply de infra).
#
# Los nombres del state remoto se referencian desde el módulo state-backend para
# acotar la política de SOLO LECTURA del rol plan al state/lock REALES.
# Tags lógicos minúscula (F3) además de los default_tags capitalizados de la raíz.
# ─────────────────────────────────────────────────────────────────────────────
module "iam_github_oidc" {
  source = "../../modules/iam-github-oidc"

  github_org  = "jlsaco"
  github_repo = "cam-counter"

  # El proveedor OIDC NO existe aún en la cuenta → lo crea este módulo. Si en el
  # futuro existiera fuera del state, resolver de forma PERSISTENTE (import al
  # state o create_oidc_provider=false + oidc_provider_arn aquí), NO por CLI.
  create_oidc_provider = true

  tfstate_bucket_name     = module.state_backend.state_bucket_name
  tfstate_lock_table_name = module.state_backend.lock_table_name

  tags = {
    project    = "cam-counter"
    managed_by = "mad-runner"
  }
}
