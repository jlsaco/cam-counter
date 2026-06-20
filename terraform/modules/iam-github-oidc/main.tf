# ─────────────────────────────────────────────────────────────────────────────
# GitHub Actions OIDC provider + DOS roles IAM SEPARADOS (plan vs deploy).
#
# Modelo de DOS ACTORES (ver CLAUDE.md §5 y README.md de este módulo):
#   - RUNNER MAD: aplica la INFRAESTRUCTURA de forma autónoma con las credenciales
#     de SU ENTORNO (jamás commiteadas). NO usa estos roles.
#   - GitHub Actions CI: SOLO-PLAN. Asume el rol `plan` vía OIDC (web identity),
#     ejecuta `terraform plan` de SOLO LECTURA y NUNCA hace `apply` de infra.
#
# El rol `deploy` queda CREADO para operación futura (p.ej. workflows de
# release/promote que publican OBJETOS S3 — eso NO es `terraform apply` de infra),
# gated a `environment:prod`/`main`/tags y NUNCA asumible desde `pull_request`.
#
# Tags (F3): cada recurso lleva los tags lógicos en MINÚSCULA `project`/`managed_by`
# (vía `var.tags`) MÁS los `default_tags` capitalizados heredados de la raíz prod.
# ─────────────────────────────────────────────────────────────────────────────

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

locals {
  oidc_url      = "https://token.actions.githubusercontent.com"
  oidc_host     = "token.actions.githubusercontent.com"
  account_id    = data.aws_caller_identity.current.account_id
  region        = data.aws_region.current.name
  repo_sub_base = "repo:${var.github_org}/${var.github_repo}"

  # ARN del proveedor OIDC: el creado por este módulo, o el pasado por variable
  # cuando `create_oidc_provider = false` (proveedor preexistente). Resolución
  # PERSISTENTE: no depende de flags efímeros de CLI.
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.this[0].arn : var.oidc_provider_arn

  # ARNs del estado remoto compartido (para acotar la política del rol plan).
  tfstate_bucket_arn = "arn:aws:s3:::${var.tfstate_bucket_name}"
  tfstate_lock_arn   = "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.tfstate_lock_table_name}"

  # Thumbprints ESTÁTICOS y conocidos del IdP de GitHub Actions. Elección
  # documentada en README (idempotencia/persistencia F1, sin red ni drift; AWS
  # ya no valida el thumbprint para este IdP well-known, sólo es campo requerido).
  github_oidc_thumbprints = [
    "6938fd4d98bab03faadb97b34396831e3780aea1",
    "1c58a3a8518e8759bf075b76b750d4f2df264fcc",
  ]
}

# ─────────────────────────────────────────────────────────────────────────────
# Proveedor OIDC de GitHub Actions (creado condicionalmente).
# ─────────────────────────────────────────────────────────────────────────────
resource "aws_iam_openid_connect_provider" "this" {
  count = var.create_oidc_provider ? 1 : 0

  url             = local.oidc_url
  client_id_list  = ["sts.amazonaws.com"]
  thumbprint_list = local.github_oidc_thumbprints

  tags = merge(var.tags, {
    Name = "github-actions-oidc"
  })
}

# ─────────────────────────────────────────────────────────────────────────────
# Trust del rol PLAN: AssumeRoleWithWebIdentity desde el OIDC de GitHub, acotado a
# `aud = sts.amazonaws.com` y `sub` en `pull_request` y `main` del repo curado.
# ─────────────────────────────────────────────────────────────────────────────
data "aws_iam_policy_document" "plan_trust" {
  statement {
    sid     = "GitHubOIDCAssumePlan"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_host}:sub"
      values = [
        "${local.repo_sub_base}:pull_request",
        "${local.repo_sub_base}:ref:refs/heads/main",
      ]
    }
  }
}

# Política del rol PLAN: SOLO LECTURA para `terraform plan` + acceso al estado
# remoto. La ÚNICA escritura permitida es Put/Delete en la tabla de lock (acotada
# por ARN), imprescindible para adquirir/soltar el lock del plan. NO incluye
# ninguna acción de creación/modificación/borrado de recursos de producto.
data "aws_iam_policy_document" "plan_permissions" {
  statement {
    sid    = "ReadOnlyForTerraformPlan"
    effect = "Allow"
    actions = [
      "iam:Get*",
      "iam:List*",
      "s3:Get*",
      "s3:List*",
      "dynamodb:Describe*",
      "dynamodb:List*",
      "dynamodb:GetItem",
      "sts:GetCallerIdentity",
    ]
    resources = ["*"]
  }

  statement {
    sid    = "TerraformPlanLockWrite"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
    ]
    resources = [local.tfstate_lock_arn]
  }
}

resource "aws_iam_role" "plan" {
  name                 = var.plan_role_name
  description          = "GitHub Actions OIDC — SOLO LECTURA (terraform plan en PRs y main). CI plan-only."
  assume_role_policy   = data.aws_iam_policy_document.plan_trust.json
  max_session_duration = 3600

  tags = merge(var.tags, {
    Name = var.plan_role_name
  })
}

resource "aws_iam_role_policy" "plan" {
  name   = "${var.plan_role_name}-readonly"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan_permissions.json
}

# ─────────────────────────────────────────────────────────────────────────────
# Trust del rol DEPLOY: igual estructura, pero `sub` SÓLO en `environment:prod`,
# `main` y tags. NUNCA `pull_request` (un PR no debe poder asumir deploy).
# ─────────────────────────────────────────────────────────────────────────────
data "aws_iam_policy_document" "deploy_trust" {
  statement {
    sid     = "GitHubOIDCAssumeDeploy"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "${local.oidc_host}:aud"
      values   = ["sts.amazonaws.com"]
    }

    condition {
      test     = "StringLike"
      variable = "${local.oidc_host}:sub"
      values = [
        "${local.repo_sub_base}:environment:prod",
        "${local.repo_sub_base}:ref:refs/heads/main",
        "${local.repo_sub_base}:ref:refs/tags/*",
      ]
    }
  }
}

# Política del rol DEPLOY: permisos de `apply` acotados (espíritu de mínimo
# privilegio) al estado remoto + recursos del prefijo `cam-counter-`. Uso
# operativo futuro; en esta pila el apply de infra lo hace el RUNNER MAD.
data "aws_iam_policy_document" "deploy_permissions" {
  # Estado remoto compartido: lectura/escritura completas del state y del lock.
  statement {
    sid    = "TerraformStateFull"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketVersioning",
    ]
    resources = [
      local.tfstate_bucket_arn,
      "${local.tfstate_bucket_arn}/*",
    ]
  }

  statement {
    sid    = "TerraformLockFull"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [local.tfstate_lock_arn]
  }

  # Recursos S3 del producto (media/releases — creados en PRs posteriores).
  statement {
    sid     = "ProductS3"
    effect  = "Allow"
    actions = ["s3:*"]
    resources = [
      "arn:aws:s3:::${var.github_repo}-*",
      "arn:aws:s3:::${var.github_repo}-*/*",
    ]
  }

  # Tablas DynamoDB del producto (events/devices — creadas en PRs posteriores).
  statement {
    sid     = "ProductDynamoDB"
    effect  = "Allow"
    actions = ["dynamodb:*"]
    resources = [
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.github_repo}-*",
      "arn:aws:dynamodb:${local.region}:${local.account_id}:table/${var.github_repo}-*/index/*",
    ]
  }

  # IAM del producto (roles/políticas con prefijo cam-counter-, p.ej. per-Pi).
  statement {
    sid     = "ProductIAM"
    effect  = "Allow"
    actions = ["iam:*"]
    resources = [
      "arn:aws:iam::${local.account_id}:role/${var.github_repo}-*",
      "arn:aws:iam::${local.account_id}:policy/${var.github_repo}-*",
    ]
  }

  # Lectura global necesaria para refresh/plan durante el apply.
  statement {
    sid    = "DeployReadOnlyGlobal"
    effect = "Allow"
    actions = [
      "iam:Get*",
      "iam:List*",
      "s3:Get*",
      "s3:List*",
      "dynamodb:Describe*",
      "dynamodb:List*",
      "sts:GetCallerIdentity",
    ]
    resources = ["*"]
  }
}

resource "aws_iam_role" "deploy" {
  name                 = var.deploy_role_name
  description          = "GitHub Actions OIDC — apply (uso operativo futuro). Gated a environment:prod/main/tags; NUNCA pull_request."
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600

  tags = merge(var.tags, {
    Name = var.deploy_role_name
  })
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${var.deploy_role_name}-apply"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy_permissions.json
}
