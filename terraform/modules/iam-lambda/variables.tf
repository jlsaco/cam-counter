# Variables del módulo `iam-lambda` — rol de ejecución + política inline least-privilege
# POR FUNCIÓN Lambda (un rol por función, NUNCA compartido).
#
# La política se PARAMETRIZA por los ARNs de los recursos a los que cada función tiene
# acceso acotado. Todos los accesos son OPT-IN: una variable vacía / lista vacía OMITE por
# completo el statement correspondiente (no se concede nada por defecto salvo logs).

variable "function_short_name" {
  description = <<-EOT
    Slug `{dominio}-{accion}` de la función (p. ej. `events-ingest`, `devices-register`,
    `line-publish`). Deriva el nombre canónico de Lambda `cam-counter-{function_short_name}`
    y, según `docs/naming-standard.md` §5/§11, el rol `cam-counter-{function_short_name}-role`
    y la política `cam-counter-{function_short_name}-policy`. NO incluye el prefijo
    `cam-counter-` ni el sufijo `-role`: se componen aquí (gate de coherencia HCL↔doc).
  EOT
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,62}$", var.function_short_name))
    error_message = "function_short_name debe ser un slug ASCII minúscula que cumpla ^[a-z0-9][a-z0-9-]{1,62}$ (kebab, sin '#' ni '/'); p. ej. 'events-ingest'."
  }
}

variable "name_prefix" {
  description = "Prefijo de producto del nombre del rol/política (canon `cam-counter-`)."
  type        = string
  default     = "cam-counter"

  validation {
    condition     = can(regex("^cam-counter", var.name_prefix))
    error_message = "name_prefix debe empezar por el prefijo de producto 'cam-counter'."
  }
}

# ───────────────────────── DynamoDB (opt-in por ARN) ─────────────────────────
variable "dynamodb_table_arns" {
  description = <<-EOT
    ARNs de las tablas DynamoDB a las que la función puede acceder. Lista VACÍA (default)
    OMITE por completo el statement de DynamoDB (la función no toca DynamoDB). Acota el
    acceso EXACTAMENTE a estos ARNs (least-privilege: nunca `*`).
  EOT
  type        = list(string)
  default     = []
}

variable "dynamodb_actions" {
  description = <<-EOT
    Acciones DynamoDB concedidas sobre `dynamodb_table_arns` (+ `dynamodb_gsi_arns`). Default
    mínimo de ingesta/heartbeat: PutItem + UpdateItem. NO incluye `Scan` ni `DeleteItem` por
    defecto (least-privilege); añádelos explícitamente sólo si la función los necesita.
  EOT
  type        = list(string)
  default     = ["dynamodb:PutItem", "dynamodb:UpdateItem"]
}

variable "dynamodb_gsi_arns" {
  description = <<-EOT
    ARNs de índices secundarios (GSI/LSI) que la función consulta, p. ej.
    `arn:aws:dynamodb:...:table/cam-counter-devices/index/GSI1`. Se añaden como recursos del
    statement de DynamoDB junto a `dynamodb_table_arns` (un Query sobre un GSI exige el ARN
    del índice, no sólo el de la tabla). Lista VACÍA (default) = sin acceso a índices.
  EOT
  type        = list(string)
  default     = []
}

# ───────────────────────── S3 (opt-in por bucket) ─────────────────────────
variable "s3_bucket_arn" {
  description = <<-EOT
    ARN del bucket S3 al que la función accede (p. ej. el bucket de media). Vacío (default)
    OMITE por completo el statement de S3. El acceso se acota al prefijo `s3_prefix` dentro
    del bucket y exige `aws:SecureTransport = true` (sólo TLS).
  EOT
  type        = string
  default     = ""
}

variable "s3_prefix" {
  description = <<-EOT
    Prefijo (key pattern) dentro de `s3_bucket_arn` al que se acota el acceso S3. Default
    `media/*` (convención de claves de media de CLAUDE.md §7). El recurso efectivo es
    `$${s3_bucket_arn}/$${s3_prefix}`.
  EOT
  type        = string
  default     = "media/*"
}

variable "s3_actions" {
  description = <<-EOT
    Acciones S3 concedidas sobre `$${s3_bucket_arn}/$${s3_prefix}`. Default `s3:GetObject`
    (sólo lectura, p. ej. clip-presign generando URLs de descarga). NO incluye `PutObject`
    ni `DeleteObject` por defecto (least-privilege).
  EOT
  type        = list(string)
  default     = ["s3:GetObject"]
}

# ───────────────────────── SQS DLQ (opt-in por ARN) ─────────────────────────
variable "sqs_dlq_arn" {
  description = <<-EOT
    ARN de la cola SQS usada como dead-letter queue de la función. Vacío (default) OMITE el
    statement de SQS. Si se indica, concede ÚNICAMENTE `sqs:SendMessage` sobre esa cola
    (lo que Lambda necesita para depositar invocaciones fallidas en la DLQ).
  EOT
  type        = string
  default     = ""
}

# ───────────────────────── X-Ray (opt-in por flag) ─────────────────────────
variable "enable_xray" {
  description = <<-EOT
    Si true, concede el tracing activo de X-Ray (`xray:PutTraceSegments`,
    `xray:PutTelemetryRecords`). Default false. Las acciones de X-Ray no admiten permisos a
    nivel de recurso, por lo que su recurso es `*` (acotación inherente del servicio).
  EOT
  type        = bool
  default     = false
}

# ───────────────────────── Statements extra (escotilla controlada) ─────────────────────────
variable "extra_policy_statements" {
  description = <<-EOT
    Statements adicionales least-privilege para necesidades específicas de una función que no
    cubren las variables tipadas (DynamoDB/S3/SQS/X-Ray/logs). Cada elemento DEBE acotar sus
    `resources` (no usar `*` salvo servicios que no admiten resource-level). Lista VACÍA
    (default) = ningún statement extra. Es una escotilla EXPLÍCITA y revisable, no un atajo
    para conceder acceso amplio.
  EOT
  type = list(object({
    sid       = optional(string, "")
    effect    = optional(string, "Allow")
    actions   = list(string)
    resources = list(string)
  }))
  default = []
}

# ───────────────────────── Contexto de cuenta/región (ARN de logs) ─────────────────────────
variable "aws_account_id" {
  description = "ID de la cuenta AWS. Se usa para construir el ARN acotado del log group de la función."
  type        = string
  default     = "950639281773"
}

variable "aws_region" {
  description = "Región AWS. Se usa para construir el ARN acotado del log group de la función."
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3): se mergean en el rol para GARANTIZAR la presencia de
    `managed_by = "mad-runner"` y `project = "cam-counter"`. AWS IAM trata las claves de tag
    como CASE-INSENSITIVE: por eso la raíz instancia este módulo con el proveedor IAM-safe
    `aws.iam` (default_tags sin las claves capitalizadas que colisionarían). NUNCA usar la
    clave capitalizada `ManagedBy` con valor "mad-runner".
  EOT
  type        = map(string)
  default     = {}
}
