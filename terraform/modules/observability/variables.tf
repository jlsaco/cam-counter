# Variables del módulo `observability` (WP18 — cierre).
#
# Todos los identificadores externos (Lambdas, IoT Rule, API HTTP, DLQ) son STRINGS con
# defaults = nombres reales del producto. Las alarmas de CloudWatch referencian recursos por
# DIMENSIÓN (nombre), NO por referencia Terraform: por eso este módulo es AUTOCONTENIDO y
# ADITIVO (F1) — se puede aplicar aunque algún recurso destino aún no exista (la alarma queda
# en INSUFFICIENT_DATA, nunca rompe el plan/apply). Las alarmas de API y DLQ se crean SÓLO si
# su identificador está presente (count), para no fabricar alarmas que nunca podrán resolverse.

variable "region" {
  description = "Región AWS (debe coincidir con la del provider)."
  type        = string
  default     = "us-east-1"
}

variable "dashboard_name" {
  description = "Nombre del dashboard de CloudWatch de la flota."
  type        = string
  default     = "cam-counter-fleet"
}

# ───────────────────────── Lambdas (dimensión FunctionName) ─────────────────────────

variable "ingest_lambda_name" {
  description = "Nombre de la Lambda de ingesta de CrossingEvents (destino de la IoT Rule de cruces)."
  type        = string
  default     = "cam-counter-events-ingest"
}

variable "fleet_api_lambda_name" {
  description = "Nombre de la Lambda fleet-api (read-only, detrás del authorizer JWT)."
  type        = string
  default     = "cam-counter-fleet-api"
}

variable "clip_presign_lambda_name" {
  description = "Nombre de la Lambda clip-presign (URLs prefirmadas de clips S3)."
  type        = string
  default     = "cam-counter-clip-presign"
}

# ───────────────────────── IoT Rule de cruces (dimensión RuleName) ─────────────────────────

variable "ingest_iot_rule_name" {
  description = "Nombre de la IoT Topic Rule que enruta cruces (cam-counter/{device}/events/crossing) a la Lambda de ingesta. Las IoT Rule names usan guion_bajo."
  type        = string
  default     = "cam_counter_events_crossing"
}

# ───────────────────────── API Gateway HTTP API v2 (dimensión ApiId) ─────────────────────────

variable "api_id" {
  description = "ApiId del HTTP API v2 de la fleet-api. Vacío => no se crean alarmas de API (additivo, sin alarmas huérfanas)."
  type        = string
  default     = ""
}

# ───────────────────────── DLQ de la ingesta (dimensión QueueName) ─────────────────────────

variable "dlq_name" {
  description = "Nombre de la cola SQS DLQ de la Lambda de ingesta. Vacío => no se crea la alarma de profundidad de DLQ."
  type        = string
  default     = ""
}

# ───────────────────────── Status path / IoT Lifecycle Events ─────────────────────────

variable "device_status_table_name" {
  description = "Tabla DynamoDB de estado de presencia de la flota (respaldo NO opcional del LWT). La CREA este módulo. PK=clientId."
  type        = string
  default     = "cam-counter-device-status"
}

variable "device_status_ttl_days" {
  description = "Días de retención de los eventos de presencia en la tabla de status (TTL). 0 = sin TTL."
  type        = number
  default     = 90
}

# ───────────────────────── Notificaciones (SNS) ─────────────────────────

variable "alarm_email" {
  description = "Email opcional suscrito al topic SNS de alarmas. Vacío => topic creado sin suscripción (se añade luego desde consola/CLI)."
  type        = string
  default     = ""
}

variable "sns_topic_name" {
  description = "Nombre del topic SNS de alarmas de la flota."
  type        = string
  default     = "cam-counter-alarms"
}

# ───────────────────────── Umbrales / ventanas de alarma ─────────────────────────

variable "alarm_period_seconds" {
  description = "Período (s) de evaluación de las alarmas basadas en suma de errores/throttles."
  type        = number
  default     = 300
}

variable "dlq_depth_threshold" {
  description = "Profundidad de la DLQ (mensajes visibles) que dispara la alarma."
  type        = number
  default     = 1
}

variable "tags" {
  description = "Tags lógicos adicionales (F3: garantizan la clave minúscula managed_by=mad-runner)."
  type        = map(string)
  default     = {}
}
