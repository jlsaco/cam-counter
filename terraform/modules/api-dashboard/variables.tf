# Variables del módulo `api-dashboard` — API Gateway HTTP (v2) + lambdas fleet-api / clip-presign
# + authorizer JWT Cognito. El frontend de la consola de flota NUNCA habla DynamoDB/S3 directo
# (CLAUDE.md §2): lo hace a través de esta API autenticada con los JWT del User Pool de WP10.
#
# Los ROLES de ejecución least-privilege NO los crea este módulo: se inyectan ya construidos
# (`*_role_arn`) por el módulo `iam-lambda` (WP03), instanciado una vez por función en la raíz.

variable "name_prefix" {
  description = "Prefijo de producto de los nombres de recursos (canon `cam-counter-`)."
  type        = string
  default     = "cam-counter"

  validation {
    condition     = can(regex("^cam-counter", var.name_prefix))
    error_message = "name_prefix debe empezar por el prefijo de producto 'cam-counter'."
  }
}

variable "api_name" {
  description = "Nombre de la HTTP API (apigatewayv2). Canon `cam-counter-fleet-api`."
  type        = string
  default     = "cam-counter-fleet-api"
}

variable "stage_name" {
  description = "Nombre del stage de despliegue de la HTTP API (auto_deploy)."
  type        = string
  default     = "prod"
}

variable "authorizer_name" {
  description = "Nombre del authorizer JWT Cognito de la API. Canon `cam-counter-fleet-cognito-authorizer`."
  type        = string
  default     = "cam-counter-fleet-cognito-authorizer"
}

# ───────────────────────── Authorizer JWT (Cognito, WP10) ─────────────────────────

variable "cognito_user_pool_endpoint" {
  description = <<-EOT
    Endpoint del User Pool de operadores (`cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXX`,
    output `user_pool_endpoint` del módulo `cognito`). El ISSUER del authorizer JWT es
    `https://$${cognito_user_pool_endpoint}`; los JWT de Cognito se validan contra él.
  EOT
  type        = string

  validation {
    condition     = can(regex("^cognito-idp\\.[a-z0-9-]+\\.amazonaws\\.com/[a-zA-Z0-9_-]+$", var.cognito_user_pool_endpoint))
    error_message = "cognito_user_pool_endpoint debe tener la forma 'cognito-idp.<region>.amazonaws.com/<user-pool-id>' (sin https://)."
  }
}

variable "jwt_audience" {
  description = <<-EOT
    Audiencias aceptadas por el authorizer JWT = IDs de app client de Cognito autorizados a
    llamar a la API. Incluye el client WEB (SPA, PKCE) y el client de TEST (validación curl +
    JWT de WP10). El authorizer valida la claim `aud` (ID token) o `client_id` (access token de
    Cognito) contra esta lista.
  EOT
  type        = list(string)

  validation {
    condition     = length(var.jwt_audience) > 0
    error_message = "jwt_audience no puede estar vacío: el authorizer JWT exige al menos un app client ID."
  }
}

# ───────────────────────── Roles de ejecución (iam-lambda, WP03) ─────────────────────────

variable "fleet_api_role_arn" {
  description = "ARN del rol de ejecución least-privilege de `cam-counter-fleet-api` (Query/GetItem read-only sobre events/devices). Lo crea `iam-lambda` en la raíz."
  type        = string
}

variable "clip_presign_role_arn" {
  description = "ARN del rol de ejecución least-privilege de `cam-counter-clip-presign` (s3:GetObject sobre media/*). Lo crea `iam-lambda` en la raíz."
  type        = string
}

# ───────────────────────── Recursos del plano de datos (read-only) ─────────────────────────

variable "events_table_name" {
  description = "Nombre de la tabla DynamoDB de eventos de cruce (`cam-counter-events`)."
  type        = string
}

variable "devices_table_name" {
  description = "Nombre de la tabla DynamoDB de registro de dispositivos (`cam-counter-devices`)."
  type        = string
}

variable "gsi1_name" {
  description = "Nombre del GSI1 (por canal) del registro de dispositivos que consulta GET /devices."
  type        = string
  default     = "GSI1"
}

variable "media_bucket_name" {
  description = "Nombre del bucket S3 de media. `clip-presign` firma GET sobre `media/*` de este bucket."
  type        = string
}

variable "presign_ttl_seconds" {
  description = "TTL (segundos) de las presigned URL GET emitidas por `clip-presign`. Default 300s (5 min)."
  type        = number
  default     = 300

  validation {
    condition     = var.presign_ttl_seconds > 0 && var.presign_ttl_seconds <= 3600
    error_message = "presign_ttl_seconds debe estar en (0, 3600]."
  }
}

# ───────────────────────── CORS (dominio Amplify; placeholder hasta WP13) ─────────────────────────

variable "cors_allow_origins" {
  description = <<-EOT
    Orígenes permitidos por CORS de la HTTP API. PLACEHOLDER del dominio Amplify de la consola
    (WP13 lo reconcilia con un update IN-PLACE del API, no un replace). El authorizer JWT es la
    barrera real de autenticación; CORS sólo acota qué orígenes de navegador pueden invocar.
  EOT
  type        = list(string)
  default     = ["https://localhost:5173"]
}

# ───────────────────────── Empaquetado de las Lambdas ─────────────────────────

variable "fleet_api_source_dir" {
  description = "Directorio fuente del paquete `fleet_api`. Vacío (default) = `$${path.module}/../../../lambdas/fleet_api`."
  type        = string
  default     = ""
}

variable "clip_presign_source_dir" {
  description = "Directorio fuente del paquete `clip_presign`. Vacío (default) = `$${path.module}/../../../lambdas/clip_presign`."
  type        = string
  default     = ""
}

variable "lambda_runtime" {
  description = "Runtime de las funciones Lambda (Python)."
  type        = string
  default     = "python3.12"
}

variable "lambda_timeout_seconds" {
  description = "Timeout (segundos) de las funciones Lambda."
  type        = number
  default     = 15
}

variable "lambda_memory_mb" {
  description = "Memoria (MB) de las funciones Lambda."
  type        = number
  default     = 256
}

variable "log_retention_days" {
  description = "Retención (días) de los log groups de las Lambdas y del access log de la API."
  type        = number
  default     = 30
}

variable "aws_region" {
  description = "Región AWS (para el endpoint regional de los clientes boto3 de las Lambdas)."
  type        = string
  default     = "us-east-1"
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3) que se mergean en los recursos taggables del módulo para
    GARANTIZAR `managed_by = "mad-runner"` y `project = "cam-counter"`. Este módulo NO crea
    recursos IAM (los roles vienen de `iam-lambda`), así que usa el proveedor por defecto y los
    `default_tags` dual-case son válidos.
  EOT
  type        = map(string)
  default     = {}
}
