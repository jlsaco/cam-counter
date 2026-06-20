# ─────────────────────────────────────────────────────────────────────────────
# Variables del módulo media-bucket — bucket S3 de MEDIA del producto
# (clips / gifs / snapshots de los eventos de cruce).
#
# Es uno de los TRES buckets jamás conflados (ver CLAUDE.md §7):
#   - cam-counter-rpi-artifacts-…  → backup de binarios de ops (EXISTENTE, NO tocar)
#   - cam-counter-media-…          → ESTE módulo (media de producto)
#   - cam-counter-fleet-releases-… → artefactos OTA + manifiestos (PR11)
# ─────────────────────────────────────────────────────────────────────────────

variable "bucket_name" {
  description = "Nombre del bucket S3 de media del producto. Global único; convención de claves: media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.{ext}."
  type        = string
  default     = "cam-counter-media-950639281773"
}

variable "enable_versioning" {
  description = "Habilita el versionado del bucket de media (recomendado: true, para poder recuperar clips sobrescritos/borrados)."
  type        = bool
  default     = true
}

variable "transition_ia_days" {
  description = "Días tras los cuales los objetos de media transicionan a STANDARD_IA (almacenamiento de acceso infrecuente)."
  type        = number
  default     = 30
}

variable "expiration_days" {
  description = "Días tras los cuales los objetos de media expiran (se borran). Retención por defecto de un año."
  type        = number
  default     = 365
}

variable "abort_multipart_days" {
  description = "Días tras los cuales se abortan las subidas multipart incompletas (limpieza de uploads colgados)."
  type        = number
  default     = 7
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
