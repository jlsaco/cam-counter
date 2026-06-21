# Variables del módulo `state-backend`.
# Valores por defecto sensatos (los nombres reales del producto) pero parametrizables
# para poder reutilizar el módulo en pruebas o en otra cuenta sin tocar el HCL.

variable "state_bucket_name" {
  description = "Nombre del bucket S3 que almacena el .tfstate remoto. Prefijo cam-counter-."
  type        = string
  default     = "cam-counter-tfstate-950639281773"

  validation {
    condition     = can(regex("^cam-counter-", var.state_bucket_name))
    error_message = "El bucket de estado debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "lock_table_name" {
  description = "Nombre de la tabla DynamoDB de lock de Terraform (clave primaria LockID)."
  type        = string
  default     = "cam-counter-tfstate-lock"

  validation {
    condition     = can(regex("^cam-counter-", var.lock_table_name))
    error_message = "La tabla de lock debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "noncurrent_version_expiration_days" {
  description = "Días tras los cuales expiran las versiones NO actuales del .tfstate en el bucket versionado."
  type        = number
  default     = 90

  validation {
    condition     = var.noncurrent_version_expiration_days > 0
    error_message = "noncurrent_version_expiration_days debe ser un entero positivo."
  }
}
