# Variables del módulo `events-table`.
# Valores por defecto sensatos (nombres reales del producto) pero parametrizables.

variable "table_name" {
  description = "Nombre de la tabla DynamoDB de eventos de cruce. Prefijo cam-counter-."
  type        = string
  default     = "cam-counter-events"

  validation {
    condition     = can(regex("^cam-counter-", var.table_name))
    error_message = "La tabla de eventos debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "gsi1_name" {
  description = "Nombre del GSI por sitio (enumera eventos de todas las cámaras de un sitio por tiempo)."
  type        = string
  default     = "GSI1"
}

variable "enable_ttl" {
  description = <<-EOT
    Habilita el TTL nativo de DynamoDB (expiración automática de items por el atributo
    `ttl_attribute_name`). DESHABILITADO por defecto: el histórico de eventos en nube se
    conserva; el TTL queda como hook opcional para retención configurable.
  EOT
  type        = bool
  default     = false
}

variable "ttl_attribute_name" {
  description = "Nombre del atributo TTL (epoch en segundos UTC) cuando `enable_ttl = true`."
  type        = string
  default     = "expires_at"
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3): se mergean en la tabla para GARANTIZAR la presencia de
    `managed_by = "mad-runner"` y `project = "cam-counter"` aunque cambiaran los
    `default_tags`. NUNCA usar la clave capitalizada `ManagedBy` con valor "mad-runner".
  EOT
  type        = map(string)
  default     = {}
}
