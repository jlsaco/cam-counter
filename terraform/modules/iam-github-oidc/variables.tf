# Variables del módulo `iam-github-oidc`.
#
# Provisiona el proveedor OIDC de GitHub Actions y DOS roles SEPARADOS (plan/deploy)
# con separación estricta de privilegios. Defaults sensatos (nombres reales del
# producto) pero parametrizables para pruebas/otra cuenta sin tocar el HCL.

variable "github_org" {
  description = "Organización/usuario dueño del repo en GitHub. Acota el trust OIDC."
  type        = string
  default     = "jlsaco"
}

variable "github_repo" {
  description = "Nombre del repo en GitHub. El trust se acota a repo:<org>/<repo> (NUNCA wildcard de repo)."
  type        = string
  default     = "cam-counter"
}

variable "create_oidc_provider" {
  description = <<-EOT
    Si true (default), el módulo CREA el proveedor OIDC de GitHub Actions y lo deja en el
    state compartido (decisión PERSISTENTE: applies posteriores de la pila convergen a 0
    cambios). Ponlo a false SÓLO si el proveedor ya existe FUERA de Terraform y prefieres
    consumirlo vía `oidc_provider_arn` en lugar de importarlo al state.
  EOT
  type        = bool
  default     = true
}

variable "oidc_provider_arn" {
  description = "ARN de un proveedor OIDC preexistente. Sólo se usa cuando create_oidc_provider = false."
  type        = string
  default     = ""
}

variable "tfstate_bucket_name" {
  description = "Nombre del bucket S3 del .tfstate remoto. Acota la política read-only del rol PLAN al estado real."
  type        = string

  validation {
    condition     = can(regex("^cam-counter-", var.tfstate_bucket_name))
    error_message = "El bucket de estado debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "tfstate_lock_table_name" {
  description = "Nombre de la tabla DynamoDB de lock de Terraform. Acota el (mínimo) permiso de escritura del rol PLAN al lock real."
  type        = string

  validation {
    condition     = can(regex("^cam-counter-", var.tfstate_lock_table_name))
    error_message = "La tabla de lock debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "aws_account_id" {
  description = "ID de la cuenta AWS. Se usa para construir ARNs acotados (lock table, recursos cam-counter-*)."
  type        = string
  default     = "950639281773"
}

variable "aws_region" {
  description = "Región AWS. Se usa para construir el ARN de la tabla de lock DynamoDB."
  type        = string
  default     = "us-east-1"
}

variable "resource_prefix" {
  description = "Prefijo de nombres de los recursos del producto. Acota las políticas de deploy por ARN."
  type        = string
  default     = "cam-counter-"
}

variable "plan_role_name" {
  description = "Nombre del rol IAM de PLAN (CI plan-only, solo lectura)."
  type        = string
  default     = "cam-counter-gha-plan"
}

variable "deploy_role_name" {
  description = "Nombre del rol IAM de DEPLOY (apply; uso operativo futuro, gated por environment/main/tags)."
  type        = string
  default     = "cam-counter-gha-deploy"
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3): se mergean en TODOS los recursos del módulo para
    garantizar la presencia de `managed_by = "mad-runner"` y `project = "cam-counter"`
    aunque cambiaran los `default_tags`. NUNCA usar la clave capitalizada `ManagedBy`
    con valor "mad-runner".
  EOT
  type        = map(string)
  default     = {}
}
