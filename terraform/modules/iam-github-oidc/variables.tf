# ─────────────────────────────────────────────────────────────────────────────
# Variables del módulo iam-github-oidc.
#
# Provisiona el proveedor OIDC de GitHub Actions y DOS roles IAM SEPARADOS
# (privilegio mínimo, separación plan vs apply):
#   - <plan_role_name>   → SOLO LECTURA, asumible desde `pull_request` y `main`.
#   - <deploy_role_name> → apply, gated a `environment:prod`/`main`/tags; NUNCA
#                          asumible desde `pull_request`.
# ─────────────────────────────────────────────────────────────────────────────

variable "github_org" {
  description = "Organización/usuario dueño del repo en GitHub (claim `sub`: repo:<org>/<repo>:...)."
  type        = string
  default     = "jlsaco"
}

variable "github_repo" {
  description = "Nombre del repositorio en GitHub (claim `sub`: repo:<org>/<repo>:...)."
  type        = string
  default     = "cam-counter"
}

variable "create_oidc_provider" {
  description = <<-EOT
    Si es `true` (default), este módulo CREA el `aws_iam_openid_connect_provider`
    de GitHub Actions. Si la cuenta YA tuviera el proveedor creado fuera de este
    state, fija `create_oidc_provider = false` y pasa `oidc_provider_arn` del
    existente (decisión PERSISTENTE en HCL/tfvars commiteado, NUNCA un `-var`
    efímero de CLI) para que los applies posteriores de la pila sigan dando 0
    cambios. Alternativa equivalente: `terraform import` del proveedor al recurso
    `aws_iam_openid_connect_provider.this[0]` dejando esta variable en `true`.
  EOT
  type        = bool
  default     = true
}

variable "oidc_provider_arn" {
  description = "ARN del proveedor OIDC YA existente. Sólo se usa cuando `create_oidc_provider = false`."
  type        = string
  default     = ""
}

variable "tfstate_bucket_name" {
  description = "Nombre del bucket S3 del estado remoto de Terraform. Acota la política de SOLO LECTURA del rol plan al state real (lectura del .tfstate)."
  type        = string
}

variable "tfstate_lock_table_name" {
  description = "Nombre de la tabla DynamoDB de lock del estado remoto. El rol plan sólo puede escribir (Put/Delete) en ESTA tabla para adquirir/soltar el lock del plan."
  type        = string
}

variable "plan_role_name" {
  description = "Nombre del rol IAM de SOLO LECTURA usado por CI (plan en PRs y en main)."
  type        = string
  default     = "cam-counter-gha-plan"
}

variable "deploy_role_name" {
  description = "Nombre del rol IAM de apply (uso operativo futuro), gated a environment:prod/main/tags; NUNCA pull_request."
  type        = string
  default     = "cam-counter-gha-deploy"
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3) a aplicar en TODOS los recursos del módulo,
    típicamente `{ project = "cam-counter", managed_by = "mad-runner" }`. Se
    fusionan con los `default_tags` capitalizados de la raíz prod
    (`{ Project, ManagedBy = "terraform", Env }`). La clave en MINÚSCULA
    `managed_by` debe valer `mad-runner`; NUNCA se usa `ManagedBy = "mad-runner"`.
  EOT
  type        = map(string)
  default     = {}
}
