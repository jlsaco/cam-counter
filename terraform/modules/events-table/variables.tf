# ─────────────────────────────────────────────────────────────────────────────
# Variables del módulo events-table — tabla DynamoDB de EVENTOS de cruce
# (histórico en nube). Contrato canónico CrossingEvent (ver CLAUDE.md §8.A).
#
# Claves (sólo se declaran en DynamoDB los atributos que participan en keys/índices;
# el resto del evento es schemaless):
#   PK     = CAM#{site_id}#{device_id}#{camera_id}
#   SK     = TS#{ts_event_ms:013d}#{event_id}
#   GSI1PK = SITE#{site_id}
#   GSI1SK = TS#{ts_event_ms:013d}#{event_id}
# ─────────────────────────────────────────────────────────────────────────────

variable "table_name" {
  description = "Nombre de la tabla DynamoDB de eventos de cruce."
  type        = string
  default     = "cam-counter-events"
}

variable "gsi1_name" {
  description = "Nombre del índice secundario global por sitio (enumera eventos de TODAS las cámaras de un sitio ordenados por tiempo)."
  type        = string
  default     = "GSI1"
}

variable "enable_ttl" {
  description = "Habilita el TTL de DynamoDB sobre `ttl_attribute_name`. DESHABILITADO por defecto: el histórico de eventos no caduca salvo política explícita."
  type        = bool
  default     = false
}

variable "ttl_attribute_name" {
  description = "Nombre del atributo numérico (epoch segundos) usado como TTL cuando `enable_ttl = true`."
  type        = string
  default     = "expires_at"
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3) a aplicar en TODOS los recursos del módulo,
    típicamente `{ project = "cam-counter", managed_by = "mad-runner" }`. Se
    fusionan con los `default_tags` capitalizados de la raíz prod. La clave en
    MINÚSCULA `managed_by` vale `mad-runner`; NUNCA `ManagedBy = "mad-runner"`.
  EOT
  type        = map(string)
  default     = {}
}
