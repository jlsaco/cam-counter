# Variables del módulo `amplify-app` — AWS Amplify Hosting (WEB_COMPUTE) de la consola de
# flota Next.js (`web/dashboard`, SSR / App Router).
#
# El módulo crea UN `aws_amplify_app` (plataforma WEB_COMPUTE, monorepo `appRoot`) conectado
# al repo de GitHub vía un token OAuth/PAT que llega por `access_token` (SENSITIVE) — el token
# NUNCA se commitea: la raíz live lo lee de SSM SecureString (ver README §"Token OAuth"). Más
# UN `aws_amplify_branch` (`main`, framework Next.js SSR) con las variables públicas
# `NEXT_PUBLIC_*` (IDs de Cognito de WP10 + endpoint de la API de WP11). CERO secretos AWS en
# la app: la SPA es read-only y la API valida el JWT.

variable "app_name" {
  description = "Nombre de la Amplify App de la consola de flota (`cam-counter-fleet-console`)."
  type        = string
  default     = "cam-counter-fleet-console"

  validation {
    condition     = can(regex("^cam-counter-", var.app_name))
    error_message = "app_name debe empezar por el prefijo de producto 'cam-counter-'."
  }
}

variable "repository" {
  description = <<-EOT
    URL HTTPS del repositorio de GitHub que Amplify conecta y construye
    (`https://github.com/jlsaco/cam-counter`). Amplify reconstruye el branch `main` en cada
    push. El código del sitio vive en el monorepo bajo `app_root`.
  EOT
  type        = string
  default     = "https://github.com/jlsaco/cam-counter"

  validation {
    condition     = can(regex("^https://github\\.com/", var.repository))
    error_message = "repository debe ser una URL HTTPS de github.com."
  }
}

variable "access_token" {
  description = <<-EOT
    Token OAuth / Personal Access Token de GitHub con permiso para conectar el repo a Amplify
    (scope `repo` + admin:repo_hook). SENSITIVE: NUNCA en git. La raíz live lo inyecta desde
    SSM SecureString (`data.aws_ssm_parameter`, ver README §"Token OAuth"). Vacío sólo en
    `validate`/CI plan-only sin secreto; el `apply` real del runner MAD sí lo provee.
  EOT
  type        = string
  sensitive   = true
  default     = ""
}

variable "branch_name" {
  description = "Branch de Git que Amplify construye y publica como entorno de producción (`main`)."
  type        = string
  default     = "main"
}

variable "app_root" {
  description = <<-EOT
    Raíz del proyecto dentro del monorepo (`web/dashboard`). Se inyecta como
    `AMPLIFY_MONOREPO_APP_ROOT` y debe coincidir con `appRoot` del `build_spec`. La consola
    Next.js vive aquí; el resto del monorepo (terraform, edge, lambdas) NO se construye.
  EOT
  type        = string
  default     = "web/dashboard"
}

variable "platform" {
  description = <<-EOT
    Plataforma de hosting de la app. `WEB_COMPUTE` habilita SSR / Server Components de Next.js
    (`output: "standalone"`); `WEB` sería sólo estático. La consola usa SSR ⇒ WEB_COMPUTE.
  EOT
  type        = string
  default     = "WEB_COMPUTE"

  validation {
    condition     = contains(["WEB_COMPUTE", "WEB"], var.platform)
    error_message = "platform debe ser 'WEB_COMPUTE' (SSR) o 'WEB' (estático)."
  }
}

variable "framework" {
  description = "Framework del branch de producción. `Next.js - SSR` para App Router con SSR sobre WEB_COMPUTE."
  type        = string
  default     = "Next.js - SSR"
}

variable "stage" {
  description = "Stage del branch de Amplify (`PRODUCTION` para `main`)."
  type        = string
  default     = "PRODUCTION"

  validation {
    condition     = contains(["PRODUCTION", "BETA", "DEVELOPMENT", "EXPERIMENTAL", "PULL_REQUEST"], var.stage)
    error_message = "stage debe ser uno de PRODUCTION/BETA/DEVELOPMENT/EXPERIMENTAL/PULL_REQUEST."
  }
}

variable "enable_auto_build" {
  description = "Si Amplify reconstruye automáticamente el branch en cada push a `main`."
  type        = bool
  default     = true
}

variable "build_spec" {
  description = <<-EOT
    Build spec (amplify.yml en línea) en formato MONOREPO (`applications: [{ appRoot, ... }]`)
    para Next.js SSR. Por defecto reproduce `web/dashboard/amplify.yml`: `npm ci` → `npm run
    build`, artefactos en `.next`, cache de `node_modules` y `.next/cache`. Si el repo trae su
    propio `amplify.yml`, éste tiene precedencia; mantenerlos coherentes.
  EOT
  type        = string
  default     = <<-YAML
    version: 1
    applications:
      - appRoot: web/dashboard
        frontend:
          phases:
            preBuild:
              commands:
                - npm ci
            build:
              commands:
                - npm run build
          artifacts:
            baseDirectory: .next
            files:
              - "**/*"
          cache:
            paths:
              - node_modules/**/*
              - .next/cache/**/*
  YAML
}

# ───────────────────────── Configuración pública NEXT_PUBLIC_* ─────────────────────────
# Toda la config del cliente es PÚBLICA (IDs de Cognito + endpoint de la API). La barrera de
# autenticación real es el authorizer JWT de la API; estos valores sólo dicen a la SPA a qué
# User Pool / API hablar. NINGÚN secreto AWS entra aquí.

variable "aws_region" {
  description = "Región AWS de los recursos de la consola (`NEXT_PUBLIC_AWS_REGION`)."
  type        = string
  default     = "us-east-1"
}

variable "cognito_user_pool_id" {
  description = "ID del User Pool de operadores (output `user_pool_id` de WP10) → `NEXT_PUBLIC_COGNITO_USER_POOL_ID`."
  type        = string
}

variable "cognito_web_client_id" {
  description = "App client ID de la SPA WEB sin secret (output `web_client_id` de WP10) → `NEXT_PUBLIC_COGNITO_WEB_CLIENT_ID`."
  type        = string
}

variable "cognito_hosted_ui_domain" {
  description = "Prefijo del domain de la Hosted UI de Cognito (output `hosted_ui_domain` de WP10) → `NEXT_PUBLIC_COGNITO_HOSTED_UI_DOMAIN`. Vacío ⇒ se omite."
  type        = string
  default     = ""
}

variable "cognito_identity_pool_id" {
  description = "ID del Identity Pool (output `identity_pool_id` de WP10) → `NEXT_PUBLIC_COGNITO_IDENTITY_POOL_ID` (opcional). Vacío ⇒ se omite."
  type        = string
  default     = ""
}

variable "api_base_url" {
  description = <<-EOT
    Endpoint base de la API de flota de WP11, SIN barra final (output `api_endpoint` del módulo
    `api-dashboard`) → `NEXT_PUBLIC_API_BASE_URL`. Vacío ⇒ se omite (p. ej. mientras WP11 aún no
    expone su `api_endpoint`); la SPA muestra error de red controlado hasta que se cablee.
  EOT
  type        = string
  default     = ""
}

variable "extra_environment_variables" {
  description = "Variables de entorno adicionales del branch (se mergean sobre las `NEXT_PUBLIC_*` derivadas; ganan estas)."
  type        = map(string)
  default     = {}
}

# ───────────────────────── Dominio personalizado (opcional) ─────────────────────────

variable "custom_domain" {
  description = <<-EOT
    Dominio propio opcional (p. ej. `console.cam-counter.example`). Vacío ⇒ se usa SÓLO el
    `<branch>.<app_id>.amplifyapp.com` por defecto de Amplify (suficiente para el e2e de login).
    Si se define, crea un `aws_amplify_domain_association` con subdominio del branch.
  EOT
  type        = string
  default     = ""
}

variable "tags" {
  description = <<-EOT
    Tags lógicos en MINÚSCULA (F3): se mergean en TODOS los recursos del módulo para GARANTIZAR
    `managed_by = "mad-runner"` y `project = "cam-counter"` (además de los `default_tags` de la
    raíz). NUNCA usar la clave capitalizada `ManagedBy` con valor "mad-runner".
  EOT
  type        = map(string)
  default     = {}
}
