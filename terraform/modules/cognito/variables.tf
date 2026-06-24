# Variables del módulo `cognito` — User Pool de operadores de la consola de flota +
# domain Hosted UI + app client web (SPA, PKCE, sin secret) + app client de TEST
# (ADMIN_NO_SRP_AUTH, sin callback web) + Identity Pool + rol authenticated read-only +
# grupos operators/admins.
#
# Todos los nombres llevan el prefijo de producto `cam-counter-` y, para las identidades de
# la CONSOLA DE FLOTA cloud, el infijo `fleet-` (los distingue de la UI LOCAL del Pi, que no
# usa Cognito). Cuenta `950639281773` / `us-east-1`.

variable "user_pool_name" {
  description = <<-EOT
    Nombre del Cognito User Pool de operadores de la consola de flota
    (`cam-counter-fleet-users`). Self-signup OFF (sólo altas por admin), MFA TOTP obligatoria.
  EOT
  type        = string
  default     = "cam-counter-fleet-users"

  validation {
    condition     = can(regex("^cam-counter-", var.user_pool_name))
    error_message = "user_pool_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "domain_prefix" {
  description = <<-EOT
    Prefijo del domain de la Hosted UI de Cognito (`cam-counter-fleet-950639281773`). Debe ser
    GLOBALMENTE único dentro de la región. Incluye el account-id para garantizar unicidad.
  EOT
  type        = string
  default     = "cam-counter-fleet-950639281773"

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{0,62}$", var.domain_prefix))
    error_message = "domain_prefix debe ser un slug DNS-safe en minúscula (^[a-z0-9][a-z0-9-]{0,62}$)."
  }
}

variable "web_client_name" {
  description = <<-EOT
    Nombre del app client WEB SPA (`cam-counter-fleet-web-client`): SIN secret, Authorization
    Code + PKCE, flujos OAuth de Hosted UI. Lo consume la SPA de flota (Amplify, WP13).
  EOT
  type        = string
  default     = "cam-counter-fleet-web-client"

  validation {
    condition     = can(regex("^cam-counter-", var.web_client_name))
    error_message = "web_client_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "test_client_name" {
  description = <<-EOT
    Nombre del app client de TEST (`cam-counter-fleet-test-client`): SIN secret, SIN callback
    web, flujo ADMIN_NO_SRP_AUTH (`ALLOW_ADMIN_USER_PASSWORD_AUTH`). Sirve para validar la API
    con `curl` + JWT (AdminInitiateAuth) sin depender de Amplify ni de la Hosted UI. Resuelve
    la dependencia oculta WP11→WP13 (nota del revisor): el acceptance «curl con JWT → 200» es
    alcanzable en su propio PR.
  EOT
  type        = string
  default     = "cam-counter-fleet-test-client"

  validation {
    condition     = can(regex("^cam-counter-", var.test_client_name))
    error_message = "test_client_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "identity_pool_name" {
  description = <<-EOT
    Nombre del Cognito Identity Pool (`cam-counter-fleet-identity`). Federa los JWT del User
    Pool a credenciales AWS de CORTA VIDA (SigV4) para el rol `authenticated` read-only. NO
    permite identidades no autenticadas.
  EOT
  type        = string
  default     = "cam-counter-fleet-identity"

  validation {
    condition     = can(regex("^cam-counter-", var.identity_pool_name))
    error_message = "identity_pool_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "authenticated_role_name" {
  description = <<-EOT
    Nombre del rol IAM `authenticated` del Identity Pool (`cam-counter-fleet-auth-role`).
    Least-privilege READ-ONLY: lectura de las tablas DynamoDB de eventos/devices y GetObject
    de media, sólo sobre TLS. Trust federado a `cognito-identity.amazonaws.com` acotado al
    `aud` = id del Identity Pool y `amr` = authenticated.
  EOT
  type        = string
  default     = "cam-counter-fleet-auth-role"

  validation {
    condition     = can(regex("^cam-counter-", var.authenticated_role_name))
    error_message = "authenticated_role_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "operators_group_name" {
  description = "Nombre del grupo de operadores (`cam-counter-operators`). Aparece en el claim `cognito:groups` del JWT para autorización a nivel de app."
  type        = string
  default     = "cam-counter-operators"

  validation {
    condition     = can(regex("^cam-counter-", var.operators_group_name))
    error_message = "operators_group_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "admins_group_name" {
  description = "Nombre del grupo de administradores (`cam-counter-admins`). Mayor precedencia que operators."
  type        = string
  default     = "cam-counter-admins"

  validation {
    condition     = can(regex("^cam-counter-", var.admins_group_name))
    error_message = "admins_group_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "callback_urls" {
  description = <<-EOT
    Callback URLs (Authorization Code) del app client WEB. PLACEHOLDER del dominio Amplify por
    defecto; se RECONCILIA con un update IN-PLACE tras WP13 (el dominio real de Amplify no
    existe todavía). Cambiar `callback_urls` es un update-in-place del `aws_cognito_user_pool_client`
    (NO force-new) en el provider AWS `~> 5.x`: ver README §"Reconciliación WP13".
  EOT
  type        = list(string)
  default     = ["https://main.placeholder.amplifyapp.com/"]

  validation {
    condition     = length(var.callback_urls) > 0
    error_message = "Debe haber al menos un callback URL (Cognito lo exige cuando hay flujos OAuth de cliente)."
  }
}

variable "logout_urls" {
  description = "Logout URLs del app client WEB. PLACEHOLDER Amplify; se reconcilia in-place en WP13 (igual que callback_urls)."
  type        = list(string)
  default     = ["https://main.placeholder.amplifyapp.com/"]
}

variable "events_table_arn" {
  description = "ARN de la tabla DynamoDB de eventos de cruce (`cam-counter-events`). Acota la lectura read-only del rol authenticated a esta tabla y su GSI1."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:dynamodb:", var.events_table_arn))
    error_message = "events_table_arn debe ser un ARN de tabla DynamoDB (^arn:aws:dynamodb:)."
  }
}

variable "devices_table_arn" {
  description = "ARN de la tabla DynamoDB de registro de dispositivos (`cam-counter-devices`). Acota la lectura read-only del rol authenticated a esta tabla y su GSI1."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:dynamodb:", var.devices_table_arn))
    error_message = "devices_table_arn debe ser un ARN de tabla DynamoDB (^arn:aws:dynamodb:)."
  }
}

variable "media_bucket_arn" {
  description = "ARN del bucket S3 de media (`cam-counter-media-950639281773`). Acota `s3:GetObject` del rol authenticated al prefijo `media/*`."
  type        = string

  validation {
    condition     = can(regex("^arn:aws:s3:::cam-counter-", var.media_bucket_arn))
    error_message = "media_bucket_arn debe ser un ARN de bucket S3 con prefijo 'arn:aws:s3:::cam-counter-'."
  }
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3): se mergean en TODOS los recursos del módulo para GARANTIZAR
    `managed_by = "mad-runner"` y `project = "cam-counter"`. El rol IAM `authenticated` usa el
    proveedor IAM-safe `aws.iam` (claves de tag CASE-INSENSITIVE); los recursos de Cognito usan
    el proveedor por defecto (dual-case válido). NUNCA usar la clave capitalizada `ManagedBy`
    con valor "mad-runner".
  EOT
  type        = map(string)
  default     = {}
}
