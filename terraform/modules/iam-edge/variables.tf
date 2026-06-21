# Variables del módulo `iam-edge` — política y rol IAM least-privilege POR Pi.
#
# La política se PARAMETRIZA por `site_id` / `device_id` y por los ARNs de los recursos a los
# que el Pi tiene acceso acotado. El rol se instancia para el PRIMER Pi con placeholders NO
# sensibles; en producción se parametriza por dispositivo.

variable "site_id" {
  description = "Slug del sitio del Pi (placeholder no sensible para el primer Pi; ^[a-z0-9][a-z0-9-]{1,62}$)."
  type        = string
  default     = "sitio-demo"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,62}$", var.site_id))
    error_message = "site_id debe ser un slug ASCII minúscula que cumpla ^[a-z0-9][a-z0-9-]{1,62}$ (sin '#' ni '/')."
  }
}

variable "device_id" {
  description = "Slug del dispositivo/Pi (placeholder no sensible para el primer Pi; ^[a-z0-9][a-z0-9-]{1,62}$)."
  type        = string
  default     = "rpi-001"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,62}$", var.device_id))
    error_message = "device_id debe ser un slug ASCII minúscula que cumpla ^[a-z0-9][a-z0-9-]{1,62}$ (sin '#' ni '/')."
  }
}

variable "runner_principal_arn" {
  description = <<-EOT
    Principal ESTABLE que puede asumir el rol per-Pi (CONTRATO para PR10 — F7). Debe ser un
    ARN de IAM estable (usuario o ROL BASE), NO una sesión efímera assumed-role
    (`arn:aws:sts::...:assumed-role/<Role>/<session>`): si el caller es una sesión asumida,
    NORMALÍZALA al rol base `arn:aws:iam::<acct>:role/<Role>` antes de pasarlo. Se persiste en
    el HCL/tfvars del root (NO en un `-var` efímero) para que un apply posterior de la pila no
    rompa el trust ni recree el rol.
  EOT
  type        = string

  validation {
    condition     = can(regex("^arn:aws:iam::[0-9]{12}:(role|user)/", var.runner_principal_arn))
    error_message = "runner_principal_arn debe ser un ARN IAM estable de rol o usuario (arn:aws:iam::<acct>:role/... o :user/...), nunca una sesión sts assumed-role."
  }
}

variable "media_bucket_arn" {
  description = "ARN del bucket de media (output del módulo media-bucket). Acota s3:PutObject al prefijo del Pi."
  type        = string
}

variable "events_table_arn" {
  description = "ARN de la tabla de eventos (output del módulo events-table). Acota dynamodb:PutItem al leading-key del Pi."
  type        = string
}

variable "devices_table_arn" {
  description = "ARN de la tabla de dispositivos (output del módulo device-registry). Acota Get/UpdateItem a la fila del Pi."
  type        = string
}

variable "releases_bucket_name" {
  description = <<-EOT
    Nombre del bucket de releases OTA (lo crea PR11; aquí SÓLO se referencia su ARN para
    conceder lectura SigV4 sobre releases/* y channels/*). Referenciar su ARN en una política
    NO requiere que el bucket exista todavía.
  EOT
  type        = string
  default     = "cam-counter-fleet-releases-950639281773"

  validation {
    condition     = can(regex("^cam-counter-", var.releases_bucket_name))
    error_message = "El bucket de releases debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "name_prefix" {
  description = "Prefijo de nombre del rol y la política per-Pi (prefijo de producto cam-counter-)."
  type        = string
  default     = "cam-counter-edge"

  validation {
    condition     = can(regex("^cam-counter-", var.name_prefix))
    error_message = "name_prefix debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "max_session_duration" {
  description = "Duración máxima de la sesión STS del rol per-Pi (segundos). Corta vida: credenciales efímeras."
  type        = number
  default     = 3600
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3): se mergean en el rol y la política para GARANTIZAR la
    presencia de `managed_by = "mad-runner"` y `project = "cam-counter"`. AWS IAM trata las
    claves de tag como CASE-INSENSITIVE: por eso la raíz instancia este módulo con el proveedor
    IAM-safe `aws.iam` (default_tags sin las claves capitalizadas que colisionarían). NUNCA usar
    la clave capitalizada `ManagedBy` con valor "mad-runner".
  EOT
  type        = map(string)
  default     = {}
}
