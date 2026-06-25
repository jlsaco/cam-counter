# Módulo `amplify-app` — AWS Amplify Hosting (WEB_COMPUTE) de la consola de flota Next.js.
#
# Recursos:
#   - aws_amplify_app    : la app (plataforma WEB_COMPUTE, monorepo appRoot, build_spec SSR,
#                          conexión al repo de GitHub vía token OAuth/PAT SENSITIVE).
#   - aws_amplify_branch : el branch `main` (entorno PRODUCTION, framework Next.js SSR) con las
#                          variables públicas `NEXT_PUBLIC_*` (Cognito WP10 + API WP11).
#   - aws_amplify_domain_association : SÓLO si se define `custom_domain` (opcional).
#
# F3 — tags: `var.tags` (minúscula `project`/`managed_by = "mad-runner"`) se mergean sobre los
# `default_tags` de la raíz. Amplify no es IAM ⇒ proveedor por defecto (dual-case válido).

locals {
  # Variables de entorno a NIVEL DE APP: el appRoot del monorepo (Amplify detecta el subdir a
  # construir) más cualquier extra de app. Se heredan en el branch.
  app_environment_variables = {
    AMPLIFY_MONOREPO_APP_ROOT = var.app_root
  }

  # Config pública obligatoria de la SPA (siempre presente).
  required_public_env = {
    NEXT_PUBLIC_AWS_REGION            = var.aws_region
    NEXT_PUBLIC_COGNITO_USER_POOL_ID  = var.cognito_user_pool_id
    NEXT_PUBLIC_COGNITO_WEB_CLIENT_ID = var.cognito_web_client_id
  }

  # Config pública OPCIONAL: sólo se inyecta la que tiene valor (un valor vacío en Amplify no
  # aporta y ensucia el diff). `NEXT_PUBLIC_API_BASE_URL` se omite mientras WP11 no exponga su
  # endpoint; la Hosted UI / Identity Pool son opcionales según el modo de auth de la SPA.
  optional_public_env = merge(
    var.api_base_url != "" ? { NEXT_PUBLIC_API_BASE_URL = var.api_base_url } : {},
    var.cognito_hosted_ui_domain != "" ? { NEXT_PUBLIC_COGNITO_HOSTED_UI_DOMAIN = var.cognito_hosted_ui_domain } : {},
    var.cognito_identity_pool_id != "" ? { NEXT_PUBLIC_COGNITO_IDENTITY_POOL_ID = var.cognito_identity_pool_id } : {},
  )

  # Variables del branch `main`: públicas derivadas + extras del llamador (estas ganan).
  branch_environment_variables = merge(
    local.required_public_env,
    local.optional_public_env,
    var.extra_environment_variables,
  )
}

resource "aws_amplify_app" "console" {
  name        = var.app_name
  repository  = var.repository
  platform    = var.platform
  oauth_token = var.access_token

  # Build spec MONOREPO (formato `applications:` con appRoot). Coherente con
  # `web/dashboard/amplify.yml`. Si el repo trae su propio amplify.yml, éste tiene precedencia.
  build_spec = var.build_spec

  # Sólo se construye el branch `main` definido explícitamente; nada de ramas/PRs automáticos.
  enable_branch_auto_build      = var.enable_auto_build
  enable_auto_branch_creation   = false
  enable_branch_auto_deletion   = false

  environment_variables = local.app_environment_variables

  tags = var.tags
}

resource "aws_amplify_branch" "main" {
  app_id      = aws_amplify_app.console.id
  branch_name = var.branch_name

  framework         = var.framework
  stage             = var.stage
  enable_auto_build = var.enable_auto_build

  environment_variables = local.branch_environment_variables

  tags = var.tags
}

# Dominio propio OPCIONAL. Por defecto NO se crea: basta el dominio
# `<branch>.<app_id>.amplifyapp.com` de Amplify para el e2e de login PKCE.
resource "aws_amplify_domain_association" "custom" {
  count = var.custom_domain != "" ? 1 : 0

  app_id      = aws_amplify_app.console.id
  domain_name = var.custom_domain

  sub_domain {
    branch_name = aws_amplify_branch.main.branch_name
    prefix      = ""
  }
}
