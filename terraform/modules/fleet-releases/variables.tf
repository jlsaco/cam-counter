# Variables del módulo `fleet-releases`.
# Valor por defecto = el nombre real del producto, pero parametrizable para reutilizar el
# módulo en pruebas o en otra cuenta sin tocar el HCL.

variable "bucket_name" {
  description = "Nombre EXACTO del bucket S3 de releases OTA + manifiestos de canal. Prefijo cam-counter-."
  type        = string
  default     = "cam-counter-fleet-releases-950639281773"

  validation {
    condition     = can(regex("^cam-counter-", var.bucket_name))
    error_message = "El bucket de releases debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "enable_versioning" {
  description = "Versionado del bucket de releases. Recomendado ON: protege el manifiesto single-writer (If-Match) ante sobreescritura y deja histórico auditable."
  type        = bool
  default     = true
}

variable "abort_multipart_days" {
  description = "Días tras los cuales se abortan los multipart uploads incompletos (limpieza de subidas a medias). No se expira el contenido del bucket."
  type        = number
  default     = 7

  validation {
    condition     = var.abort_multipart_days > 0
    error_message = "abort_multipart_days debe ser un entero positivo."
  }
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3): se mergean en el bucket para GARANTIZAR la presencia de
    `managed_by = "mad-runner"` y `project = "cam-counter"` aunque cambiaran los
    `default_tags` de la raíz. NUNCA usar la clave capitalizada `ManagedBy` con valor
    "mad-runner".
  EOT
  type        = map(string)
  default     = {}
}
