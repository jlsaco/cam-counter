# Variables del módulo `media-bucket`.
# Valores por defecto sensatos (los nombres reales del producto) pero parametrizables
# para reutilizar el módulo en pruebas o en otra cuenta sin tocar el HCL.

variable "bucket_name" {
  description = "Nombre del bucket S3 de media del producto (clips/gifs/snapshots). Prefijo cam-counter-."
  type        = string
  default     = "cam-counter-media-950639281773"

  validation {
    condition     = can(regex("^cam-counter-", var.bucket_name))
    error_message = "El bucket de media debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "enable_versioning" {
  description = "Versionado del bucket de media. Recomendado ON: protege ante sobreescritura/borrado accidental de clips."
  type        = bool
  default     = true
}

variable "transition_ia_days" {
  description = "Días tras los cuales los objetos transicionan a STANDARD_IA (almacenamiento más barato para media fría)."
  type        = number
  default     = 30

  validation {
    condition     = var.transition_ia_days > 0
    error_message = "transition_ia_days debe ser un entero positivo."
  }
}

variable "expiration_days" {
  description = "Días tras los cuales expiran (se borran) los objetos de media. Retención del histórico de clips."
  type        = number
  default     = 365

  validation {
    condition     = var.expiration_days > var.transition_ia_days
    error_message = "expiration_days debe ser mayor que transition_ia_days."
  }
}

variable "abort_multipart_days" {
  description = "Días tras los cuales se abortan los multipart uploads incompletos (limpieza de subidas a medias)."
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
