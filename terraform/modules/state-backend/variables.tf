# Variables del módulo state-backend. Defaults sensatos (los nombres reales del
# producto) pero parametrizables para tests o reutilización.

variable "state_bucket_name" {
  description = "Nombre del bucket S3 que almacena el estado remoto de Terraform (un único state de producción compartido por toda la pila de infra)."
  type        = string
  default     = "cam-counter-tfstate-950639281773"
}

variable "lock_table_name" {
  description = "Nombre de la tabla DynamoDB de lock de concurrencia de Terraform (hash_key LockID)."
  type        = string
  default     = "cam-counter-tfstate-lock"
}

variable "noncurrent_version_expiration_days" {
  description = "Días tras los cuales expiran las versiones NO-actuales del .tfstate en S3 (limpieza del versionado)."
  type        = number
  default     = 90
}
