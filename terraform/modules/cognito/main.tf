# Módulo `cognito` — auth de operadores de la CONSOLA DE FLOTA cloud (no la UI local del Pi).
#
# QUÉ RESUELVE: el dashboard de flota (SPA en Amplify, WP13) necesita autenticar operadores.
# Este módulo provisiona un Cognito User Pool de alta SÓLO por admin (self-signup OFF) con MFA
# TOTP obligatoria, su domain de Hosted UI, un app client WEB SPA sin secret (Authorization
# Code + PKCE), un app client de TEST sin callback web (ADMIN_NO_SRP_AUTH) para validar la API
# con `curl` + JWT, un Identity Pool que federa esos JWT a credenciales AWS de corta vida con
# un rol `authenticated` READ-ONLY least-privilege, y los grupos operators/admins.
#
# INDEPENDIENTE del camino IoT (Things/certs/mTLS): aquí se autentican PERSONAS, no devices.
# Apila sobre WP09; ESTRICTAMENTE ADITIVO (F1): sólo añade recursos nuevos; no toca PR02–PR04,
# PR11 ni el IoT Credentials Provider.
#
# DOS PROVEEDORES (configuration_aliases): los recursos de Cognito usan el proveedor por
# defecto `aws` (F3 dual-case completo); el rol IAM `authenticated` usa `aws.iam` (IAM-safe,
# claves de tag case-insensitive). Ver versions.tf.

locals {
  # Tags lógicos minúscula (F3) garantizados en todos los recursos del módulo.
  tags = merge(
    {
      project    = "cam-counter"
      managed_by = "mad-runner"
    },
    var.tags,
  )

  # Scopes OAuth de la Hosted UI para la SPA (OIDC estándar; sin scopes custom).
  oauth_scopes = ["openid", "email", "profile"]
}

# ════════════════════════════════ User Pool ════════════════════════════════
#
# Self-signup OFF (`allow_admin_create_user_only = true`): los operadores SÓLO se crean por
# admin (AdminCreateUser; ver scripts/cognito-create-admin.sh). MFA TOTP OBLIGATORIA
# (`mfa_configuration = "ON"` + software_token); SIN SMS (no requiere rol de SNS). Username por
# email, auto-verificado. Política de contraseña fuerte. Recuperación por email verificado.
resource "aws_cognito_user_pool" "fleet" {
  name = var.user_pool_name

  # Self-signup OFF: alta exclusivamente por administrador.
  admin_create_user_config {
    allow_admin_create_user_only = true
  }

  # MFA TOTP obligatoria; SIN SMS (evita dependencia de rol SNS).
  mfa_configuration = "ON"
  software_token_mfa_configuration {
    enabled = true
  }

  # Username = email (verificado). No hay alias adicionales.
  username_attributes      = ["email"]
  auto_verified_attributes = ["email"]

  password_policy {
    minimum_length                   = 12
    require_uppercase                = true
    require_lowercase                = true
    require_numbers                  = true
    require_symbols                  = true
    temporary_password_validity_days = 7
  }

  account_recovery_setting {
    recovery_mechanism {
      name     = "verified_email"
      priority = 1
    }
  }

  # Endurecimiento: protección frente a credenciales comprometidas en modo auditoría → enforce
  # se activaría con plan avanzado; aquí se deja el default seguro del pool.
  user_attribute_update_settings {
    attributes_require_verification_before_update = ["email"]
  }

  tags = local.tags
}

# ════════════════════════════════ Domain (Hosted UI) ════════════════════════════════
#
# Domain prefijo gestionado por Cognito (`<prefix>.auth.us-east-1.amazoncognito.com`). Sirve la
# Hosted UI de login que usa el app client WEB con Authorization Code + PKCE.
resource "aws_cognito_user_pool_domain" "fleet" {
  domain       = var.domain_prefix
  user_pool_id = aws_cognito_user_pool.fleet.id
}

# ════════════════════════════════ App client WEB (SPA, PKCE, sin secret) ════════════════════════════════
#
# SPA pública: SIN client secret (`generate_secret = false`) → Authorization Code + PKCE. Flujos
# OAuth de Hosted UI habilitados. `callback_urls`/`logout_urls` son PLACEHOLDER del dominio
# Amplify por defecto; se reconcilian con un update IN-PLACE tras WP13 (ver README).
resource "aws_cognito_user_pool_client" "web" {
  name         = var.web_client_name
  user_pool_id = aws_cognito_user_pool.fleet.id

  generate_secret = false

  # SRP para el login interactivo de la SPA + refresh. SIN flujos de password directo.
  explicit_auth_flows = [
    "ALLOW_USER_SRP_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # Authorization Code + PKCE vía Hosted UI (sólo proveedor COGNITO).
  allowed_oauth_flows_user_pool_client = true
  allowed_oauth_flows                  = ["code"]
  allowed_oauth_scopes                 = local.oauth_scopes
  supported_identity_providers         = ["COGNITO"]

  callback_urls = var.callback_urls
  logout_urls   = var.logout_urls

  # Vidas de token (defaults seguros, explícitos).
  access_token_validity  = 60
  id_token_validity      = 60
  refresh_token_validity = 30
  token_validity_units {
    access_token  = "minutes"
    id_token      = "minutes"
    refresh_token = "days"
  }

  # Endurecimiento: no revelar si un usuario existe; revocación de refresh tokens.
  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
}

# ════════════════════════════════ App client de TEST (ADMIN_NO_SRP_AUTH, sin callback web) ════════════════════════════════
#
# Resuelve la dependencia oculta WP11→WP13 (nota del revisor): permite obtener un JWT con
# `aws cognito-idp admin-initiate-auth --auth-flow ADMIN_USER_PASSWORD_AUTH` (sin SRP, sin
# Hosted UI, sin Amplify) para validar la API con `curl`. SIN secret (curl-friendly), SIN
# callback web y SIN flujos OAuth de cliente.
resource "aws_cognito_user_pool_client" "test" {
  name         = var.test_client_name
  user_pool_id = aws_cognito_user_pool.fleet.id

  generate_secret = false

  explicit_auth_flows = [
    "ALLOW_ADMIN_USER_PASSWORD_AUTH",
    "ALLOW_REFRESH_TOKEN_AUTH",
  ]

  # SIN Hosted UI: no hay flujos OAuth de cliente ni callbacks web.
  allowed_oauth_flows_user_pool_client = false

  prevent_user_existence_errors = "ENABLED"
  enable_token_revocation       = true
}

# ════════════════════════════════ Identity Pool ════════════════════════════════
#
# Federa los JWT del User Pool a credenciales AWS de corta vida (SigV4). NO permite identidades
# no autenticadas (`allow_unauthenticated_identities = false`). El client WEB y el de TEST son
# proveedores válidos (ambos del mismo User Pool).
resource "aws_cognito_identity_pool" "fleet" {
  identity_pool_name               = var.identity_pool_name
  allow_unauthenticated_identities = false

  cognito_identity_providers {
    client_id               = aws_cognito_user_pool_client.web.id
    provider_name           = aws_cognito_user_pool.fleet.endpoint
    server_side_token_check = false
  }

  cognito_identity_providers {
    client_id               = aws_cognito_user_pool_client.test.id
    provider_name           = aws_cognito_user_pool.fleet.endpoint
    server_side_token_check = false
  }

  tags = local.tags
}

# ════════════════════════════════ Rol IAM `authenticated` (read-only) ════════════════════════════════
#
# Trust FEDERADO a `cognito-identity.amazonaws.com`, acotado por `aud` = id del Identity Pool y
# `amr` = authenticated (sólo identidades AUTENTICADAS de ESTE pool pueden asumirlo). Usa el
# proveedor IAM-safe `aws.iam` (claves de tag case-insensitive).
data "aws_iam_policy_document" "authenticated_trust" {
  statement {
    sid     = "CognitoAuthenticatedAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = ["cognito-identity.amazonaws.com"]
    }

    condition {
      test     = "StringEquals"
      variable = "cognito-identity.amazonaws.com:aud"
      values   = [aws_cognito_identity_pool.fleet.id]
    }

    condition {
      test     = "ForAnyValue:StringLike"
      variable = "cognito-identity.amazonaws.com:amr"
      values   = ["authenticated"]
    }
  }
}

resource "aws_iam_role" "authenticated" {
  provider = aws.iam

  name               = var.authenticated_role_name
  description        = "Rol authenticated del Identity Pool de flota: lectura read-only (DynamoDB events/devices + GetObject media), sólo sobre TLS."
  assume_role_policy = data.aws_iam_policy_document.authenticated_trust.json

  tags = local.tags
}

# Política READ-ONLY least-privilege: SÓLO lectura de las tablas de eventos/devices (+ sus
# índices) y `s3:GetObject` del prefijo de media, todo TLS-only. SIN Put/Update/Delete.
data "aws_iam_policy_document" "authenticated_permissions" {
  statement {
    sid    = "DynamoReadOnlyTlsOnly"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:BatchGetItem",
      "dynamodb:Query",
      "dynamodb:DescribeTable",
    ]
    resources = [
      var.events_table_arn,
      "${var.events_table_arn}/index/*",
      var.devices_table_arn,
      "${var.devices_table_arn}/index/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["true"]
    }
  }

  statement {
    sid       = "MediaGetObjectTlsOnly"
    effect    = "Allow"
    actions   = ["s3:GetObject"]
    resources = ["${var.media_bucket_arn}/media/*"]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["true"]
    }
  }
}

resource "aws_iam_role_policy" "authenticated" {
  provider = aws.iam

  name   = "${var.authenticated_role_name}-policy"
  role   = aws_iam_role.authenticated.id
  policy = data.aws_iam_policy_document.authenticated_permissions.json
}

# Mapea el rol `authenticated` al Identity Pool. Sin role_mapping: toda identidad autenticada
# del pool recibe ESTE rol read-only (la autorización fina por grupo se hace a nivel de app vía
# el claim `cognito:groups`).
resource "aws_cognito_identity_pool_roles_attachment" "fleet" {
  identity_pool_id = aws_cognito_identity_pool.fleet.id

  roles = {
    authenticated = aws_iam_role.authenticated.arn
  }
}

# ════════════════════════════════ Grupos operators / admins ════════════════════════════════
#
# Aparecen en el claim `cognito:groups` del JWT para autorización a nivel de app. `precedence`
# menor = mayor prioridad: admins (1) por encima de operators (10). Se les asocia el rol
# `authenticated` (mismo rol read-only; la diferencia operador/admin se resuelve en la app).
resource "aws_cognito_user_group" "admins" {
  name         = var.admins_group_name
  user_pool_id = aws_cognito_user_pool.fleet.id
  description  = "Administradores de la consola de flota."
  precedence   = 1
  role_arn     = aws_iam_role.authenticated.arn
}

resource "aws_cognito_user_group" "operators" {
  name         = var.operators_group_name
  user_pool_id = aws_cognito_user_pool.fleet.id
  description  = "Operadores de la consola de flota."
  precedence   = 10
  role_arn     = aws_iam_role.authenticated.arn
}
