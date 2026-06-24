# Variables del módulo `iot-credential-provider` — role alias del IoT Credentials Provider
# + rol IAM `cam-counter-edge-s3-role` que el rol-alias expone.
#
# El device cambia su MISMO cert mTLS por credenciales STS de corta vida (sin segundo
# secreto). El acceso se acota por **Resource ARN** al prefijo de media del propio Pi
# (`media/{site_id}/{device_id}/*`) y exige TLS. Los slugs `site_id`/`device_id` se derivan
# del provisioning (NO de `${credentials-iot:ThingName}`, que no casa con el prefijo S3
# separado por `/`; ver README §"Por qué NO ThingName crudo").

variable "role_alias_name" {
  description = <<-EOT
    Nombre canónico del `aws_iot_role_alias` (naming-standard §5):
    `cam-counter-edge-s3-role-alias`. Es lo que el device pasa al credentials endpoint para
    cambiar su cert X.509 por credenciales STS de corta vida.
  EOT
  type        = string
  default     = "cam-counter-edge-s3-role-alias"

  validation {
    condition     = can(regex("^cam-counter-", var.role_alias_name))
    error_message = "role_alias_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "edge_s3_role_name" {
  description = <<-EOT
    Nombre canónico del rol IAM que el role alias expone (naming-standard §5/§8):
    `cam-counter-edge-s3-role`. Trust en `credentials.iot.amazonaws.com`; política sólo
    `s3:PutObject` acotada al prefijo de media del Pi y TLS-only. SIN infijo per-Pi: es el
    rol que el IoT Credentials Provider asume en nombre del cert del Thing.
  EOT
  type        = string
  default     = "cam-counter-edge-s3-role"

  validation {
    condition     = can(regex("^cam-counter-", var.edge_s3_role_name))
    error_message = "edge_s3_role_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "media_bucket_name" {
  description = <<-EOT
    Nombre del bucket S3 de media del producto (clips/gifs/snapshots). NO lo gestiona este
    módulo: se referencia (en la raíz, vía `data` source). Se usa como cross-check de que
    `media_bucket_arn` corresponde a este bucket (precondición del rol).
  EOT
  type        = string
  default     = "cam-counter-media-950639281773"

  validation {
    condition     = can(regex("^cam-counter-", var.media_bucket_name))
    error_message = "El bucket de media debe empezar por el prefijo 'cam-counter-'."
  }
}

variable "media_bucket_arn" {
  description = <<-EOT
    ARN del bucket de media (en la raíz proviene de un `data` source, NO de un recurso
    gestionado por este módulo). Acota `s3:PutObject` al prefijo `media/{site_id}/{device_id}/*`
    dentro de este bucket.
  EOT
  type        = string

  validation {
    condition     = can(regex("^arn:aws:s3:::cam-counter-", var.media_bucket_arn))
    error_message = "media_bucket_arn debe ser un ARN de bucket S3 con prefijo 'arn:aws:s3:::cam-counter-'."
  }
}

variable "site_id" {
  description = <<-EOT
    Slug del sitio del Pi del provisioning (placeholder no sensible para el primer Pi;
    ^[a-z0-9][a-z0-9-]{1,62}$). Acota el Resource ARN de `s3:PutObject` a
    `media/{site_id}/{device_id}/*`. Se deriva del provisioning, NO del ThingName crudo
    (cuyo separador `-` no casa con el separador `/` del prefijo S3; ver README).
  EOT
  type        = string
  default     = "sitio-demo"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,62}$", var.site_id))
    error_message = "site_id debe ser un slug ASCII minúscula que cumpla ^[a-z0-9][a-z0-9-]{1,62}$ (sin '#' ni '/')."
  }
}

variable "device_id" {
  description = <<-EOT
    Slug del dispositivo/Pi del provisioning (placeholder no sensible para el primer Pi;
    ^[a-z0-9][a-z0-9-]{1,62}$). Acota el Resource ARN de `s3:PutObject` a
    `media/{site_id}/{device_id}/*`.
  EOT
  type        = string
  default     = "rpi-001"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{1,62}$", var.device_id))
    error_message = "device_id debe ser un slug ASCII minúscula que cumpla ^[a-z0-9][a-z0-9-]{1,62}$ (sin '#' ni '/')."
  }
}

variable "credential_duration_seconds" {
  description = <<-EOT
    Duración (segundos) de las credenciales STS que el credentials endpoint entrega al
    device. Corta vida; default 3600. El servicio IoT exige el rango [900, 43200].
  EOT
  type        = number
  default     = 3600

  validation {
    condition     = var.credential_duration_seconds >= 900 && var.credential_duration_seconds <= 43200
    error_message = "credential_duration_seconds debe estar en el rango [900, 43200] que exige el IoT Credentials Provider."
  }
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3): se mergean en el rol y el role alias para GARANTIZAR la
    presencia de `managed_by = "mad-runner"` y `project = "cam-counter"`. AWS IAM trata las
    claves de tag como CASE-INSENSITIVE: por eso la raíz instancia este módulo con el
    proveedor IAM-safe `aws.iam` (default_tags sin las claves capitalizadas que colisionarían
    en el rol). NUNCA usar la clave capitalizada `ManagedBy` con valor "mad-runner".
  EOT
  type        = map(string)
  default     = {}
}
