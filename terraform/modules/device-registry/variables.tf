# Variables del módulo `device-registry`.
# Valores por defecto sensatos (nombres reales del producto) pero parametrizables.

variable "table_name" {
  description = "Nombre de la tabla DynamoDB de registro de dispositivos de la flota. Prefijo cam-counter-."
  type        = string
  default     = "cam-counter-devices"

  validation {
    condition     = can(regex("^cam-counter-", var.table_name))
    error_message = "La tabla de dispositivos debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "gsi1_name" {
  description = "Nombre del GSI por canal (enumera dispositivos de un canal canary/stable)."
  type        = string
  default     = "GSI1"
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
