# Módulo `iam-github-oidc` — proveedor OIDC de GitHub Actions + DOS roles SEPARADOS.
#
# Provisiona:
#   1) El proveedor OIDC `token.actions.githubusercontent.com` (un único
#      aws_iam_openid_connect_provider; creación condicional + consumo de uno preexistente).
#   2) `cam-counter-gha-plan`  — rol SOLO-LECTURA asumible desde PRs y push a main (CI plan).
#   3) `cam-counter-gha-deploy` — rol de apply gated a environment:prod/main/tags
#      (NUNCA pull_request); creado para operación futura (release/promote).
#
# SEPARACIÓN DE PRIVILEGIOS (crítico): el rol PLAN no puede aplicar recursos; el rol
# DEPLOY no es asumible desde `pull_request`. Un PR malicioso no puede invocar apply.
#
# F3 — TAGS y la restricción CASE-INSENSITIVE de AWS IAM:
# Este módulo garantiza en TODOS sus recursos la clave lógica en MINÚSCULA
# `managed_by = "mad-runner"` (y `project = "cam-counter"`) vía `local.tags`. La clave
# capitalizada `ManagedBy` NUNCA toma el valor del runner.
#
# AWS IAM trata las claves de tag como CASE-INSENSITIVE y `CreateRole` rechaza claves que
# difieren sólo en mayúsculas (Project/project, ManagedBy/managed_by: «Duplicate tag keys
# found»). El esquema F3 dual-case de los `default_tags` de la raíz —que SÍ funciona en
# S3/DynamoDB, sensibles a mayúsculas— NO puede aplicarse a recursos IAM. Por eso la raíz
# instancia ESTE módulo con un proveedor AWS dedicado `aws.iam` (pasado como su `aws` por
# defecto) cuyos `default_tags` son un subconjunto IAM-safe { Env, project=cam-counter,
# managed_by=mad-runner }. Así TODOS los recursos IAM del módulo (proveedor OIDC + ambos
# roles) cumplen la verificación F3 (clave MINÚSCULA `managed_by=mad-runner`, comprobada con
# `aws iam list-role-tags`) sin que `CreateRole` falle por colisión. El módulo en sí es de un
# único proveedor (valida en standalone); la elección del proveedor IAM-safe vive en la raíz.

locals {
  # Tags lógicos minúscula (F3) garantizados en TODOS los recursos del módulo.
  tags = merge(
    {
      project    = "cam-counter"
      managed_by = "mad-runner"
    },
    var.tags,
  )

  # `repo:<org>/<repo>` — base SIEMPRE acotada (NUNCA wildcard de repo `repo:*`).
  # Con los defaults (github_org = "jlsaco", github_repo = "cam-counter") el prefijo es
  # exactamente `repo:jlsaco/cam-counter`. Subs efectivos del trust de cada rol:
  #   PLAN  -> repo:jlsaco/cam-counter:pull_request
  #            repo:jlsaco/cam-counter:ref:refs/heads/main
  #   DEPLOY-> repo:jlsaco/cam-counter:environment:prod
  #            repo:jlsaco/cam-counter:ref:refs/heads/main
  #            repo:jlsaco/cam-counter:ref:refs/tags/*
  # `aud` SIEMPRE sts.amazonaws.com. El rol DEPLOY NUNCA admite `pull_request`.
  repo_sub_prefix = "repo:${var.github_org}/${var.github_repo}"

  # ARN del proveedor OIDC: el creado por el módulo, o el preexistente pasado por variable.
  oidc_provider_arn = var.create_oidc_provider ? aws_iam_openid_connect_provider.github_actions[0].arn : var.oidc_provider_arn

  # ARN de la tabla DynamoDB de lock (escritura MÍNIMA del rol PLAN, acotada por ARN).
  lock_table_arn = "arn:aws:dynamodb:${var.aws_region}:${var.aws_account_id}:table/${var.tfstate_lock_table_name}"
}

# ───────────────────────── Proveedor OIDC de GitHub Actions ─────────────────────────
#
# THUMBPRINT: se OMITE `thumbprint_list` a propósito (atributo Optional+Computed en el
# proveedor AWS >= v5). Desde julio de 2023 AWS asegura el endpoint
# `token.actions.githubusercontent.com` con su propia librería de CAs de confianza y COMPUTA
# el thumbprint automáticamente al crear el proveedor. Omitir el argumento es la opción MÁS
# IDEMPOTENTE: Terraform adopta el valor que AWS calcula y no hay "drift" en planes futuros
# (requisito duro F1: un plan desde checkout limpio da 0 cambios). NOTA: fijarlo a `[]`
# provocaría un diff perpetuo (AWS lo repobla en cada apply), por eso NO se fija.
#
# CREACIÓN CONDICIONAL + PERSISTENCIA: con `create_oidc_provider = true` (default) el
# proveedor se crea y queda en el STATE COMPARTIDO; applies posteriores de la pila (p.ej.
# PR04, que reaplica este root) lo ven ya en el state y convergen a 0 cambios. Si el
# proveedor ya existiera fuera de Terraform, la resolución PERSISTENTE preferida es
# `terraform import` de este recurso (manteniendo el default true); la alternativa es fijar
# `create_oidc_provider = false` + `oidc_provider_arn` en el HCL/tfvars commiteado (NUNCA un
# `-var` efímero de CLI). Ver README.md.
resource "aws_iam_openid_connect_provider" "github_actions" {
  count = var.create_oidc_provider ? 1 : 0

  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
  # thumbprint_list se OMITE: lo computa AWS (ver nota arriba). Fijarlo causaría drift.

  tags = local.tags
}

# ════════════════════════════════ Rol PLAN (CI, solo lectura) ════════════════════════════════

# Trust del rol PLAN: asumible vía OIDC desde `pull_request` y push a `main`.
data "aws_iam_policy_document" "plan_trust" {
  statement {
    sid     = "GithubActionsPlanAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    # `aud` SIEMPRE sts.amazonaws.com.
    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # `sub` acotado a los contextos de PLAN: pull_request y refs/heads/main.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "${local.repo_sub_prefix}:pull_request",
        "${local.repo_sub_prefix}:ref:refs/heads/main",
      ]
    }
  }
}

resource "aws_iam_role" "plan" {
  name                 = var.plan_role_name
  description          = "GitHub Actions CI: terraform plan SOLO-LECTURA (pull_request + main). No puede aplicar."
  assume_role_policy   = data.aws_iam_policy_document.plan_trust.json
  max_session_duration = 3600

  tags = local.tags
}

# Política PLAN: SOLO LECTURA de los recursos del producto (para `terraform plan`) + acceso
# de lectura al state remoto + escritura MÍNIMA al lock (imprescindible para adquirir/soltar
# el lock del plan). NO incluye creación/modificación/borrado de recursos de producto.
data "aws_iam_policy_document" "plan_permissions" {
  # (a) Lectura/describe para que `terraform plan` pueda refrescar el estado de los recursos
  #     de la pila (S3, DynamoDB, IAM). Sólo acciones Get/List/Describe → no muta nada.
  statement {
    sid    = "ReadOnlyRefreshForPlan"
    effect = "Allow"
    actions = [
      "s3:Get*",
      "s3:List*",
      "dynamodb:DescribeTable",
      "dynamodb:DescribeContinuousBackups",
      "dynamodb:DescribeTimeToLive",
      "dynamodb:ListTagsOfResource",
      "dynamodb:ListTables",
      "iam:Get*",
      "iam:List*",
    ]
    resources = ["*"]
  }

  # (b) Estado remoto: lectura del .tfstate en el bucket de tfstate.
  statement {
    sid    = "TfStateRead"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:ListBucket",
    ]
    resources = [
      "arn:aws:s3:::${var.tfstate_bucket_name}",
      "arn:aws:s3:::${var.tfstate_bucket_name}/*",
    ]
  }

  # (c) Lock del plan: ÚNICA escritura permitida al rol PLAN, acotada por ARN a la tabla de
  #     lock. Sin esto el plan no podría adquirir/soltar el lock de DynamoDB.
  statement {
    sid    = "TfStateLockForPlan"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [local.lock_table_arn]
  }
}

resource "aws_iam_role_policy" "plan" {
  name   = "${var.plan_role_name}-readonly"
  role   = aws_iam_role.plan.id
  policy = data.aws_iam_policy_document.plan_permissions.json
}

# ════════════════════════════ Rol DEPLOY (apply; gated, uso futuro) ════════════════════════════

# Trust del rol DEPLOY: asumible vía OIDC SÓLO desde environment:prod, push a `main` y tags.
# JAMÁS desde un Pull Request (un PR no puede invocar apply).
data "aws_iam_policy_document" "deploy_trust" {
  statement {
    sid     = "GithubActionsDeployAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRoleWithWebIdentity"]

    principals {
      type        = "Federated"
      identifiers = [local.oidc_provider_arn]
    }

    condition {
      test     = "StringEquals"
      variable = "token.actions.githubusercontent.com:aud"
      values   = ["sts.amazonaws.com"]
    }

    # `sub` gated: environment:prod, refs/heads/main y tags refs/tags/*. Sin contexto de PR.
    condition {
      test     = "StringLike"
      variable = "token.actions.githubusercontent.com:sub"
      values = [
        "${local.repo_sub_prefix}:environment:prod",
        "${local.repo_sub_prefix}:ref:refs/heads/main",
        "${local.repo_sub_prefix}:ref:refs/tags/*",
      ]
    }
  }
}

resource "aws_iam_role" "deploy" {
  name                 = var.deploy_role_name
  description          = "GitHub Actions DEPLOY (uso futuro): apply gated a environment:prod/main/tags. Jamás desde un Pull Request."
  assume_role_policy   = data.aws_iam_policy_document.deploy_trust.json
  max_session_duration = 3600

  tags = local.tags
}

# Política DEPLOY: permisos de apply acotados al prefijo `cam-counter-` donde es viable
# (mínimo privilegio) + acceso COMPLETO al estado remoto (lectura/escritura del state y del
# lock). Más amplia que PLAN pero sin salir del producto.
data "aws_iam_policy_document" "deploy_permissions" {
  # (a) Estado remoto completo: bucket de tfstate (lectura/escritura del .tfstate).
  statement {
    sid    = "TfStateReadWrite"
    effect = "Allow"
    actions = [
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:ListBucket",
      "s3:GetBucketVersioning",
    ]
    resources = [
      "arn:aws:s3:::${var.tfstate_bucket_name}",
      "arn:aws:s3:::${var.tfstate_bucket_name}/*",
    ]
  }

  # (b) Lock completo sobre la tabla de lock.
  statement {
    sid    = "TfStateLock"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:PutItem",
      "dynamodb:DeleteItem",
      "dynamodb:DescribeTable",
    ]
    resources = [local.lock_table_arn]
  }

  # (c) Buckets S3 del producto (media/releases, creados en PR04/PR11): acotado por prefijo.
  statement {
    sid    = "ProductS3Apply"
    effect = "Allow"
    actions = [
      "s3:CreateBucket",
      "s3:DeleteBucket",
      "s3:PutBucket*",
      "s3:GetBucket*",
      "s3:ListBucket",
      "s3:GetObject",
      "s3:PutObject",
      "s3:DeleteObject",
      "s3:PutEncryptionConfiguration",
      "s3:PutLifecycleConfiguration",
    ]
    resources = [
      "arn:aws:s3:::${var.resource_prefix}*",
      "arn:aws:s3:::${var.resource_prefix}*/*",
    ]
  }

  # (d) Tablas DynamoDB del producto (events/devices, PR04): acotado por prefijo.
  statement {
    sid    = "ProductDynamoDBApply"
    effect = "Allow"
    actions = [
      "dynamodb:CreateTable",
      "dynamodb:DeleteTable",
      "dynamodb:UpdateTable",
      "dynamodb:UpdateContinuousBackups",
      "dynamodb:UpdateTimeToLive",
      "dynamodb:TagResource",
      "dynamodb:UntagResource",
      "dynamodb:Describe*",
      "dynamodb:List*",
    ]
    resources = [
      "arn:aws:dynamodb:${var.aws_region}:${var.aws_account_id}:table/${var.resource_prefix}*",
      "arn:aws:dynamodb:${var.aws_region}:${var.aws_account_id}:table/${var.resource_prefix}*/index/*",
    ]
  }

  # (e) IAM del producto (rol per-Pi, políticas, OIDC): acotado por prefijo/proveedor.
  statement {
    sid    = "ProductIAMApply"
    effect = "Allow"
    actions = [
      "iam:CreateRole",
      "iam:DeleteRole",
      "iam:UpdateRole",
      "iam:UpdateAssumeRolePolicy",
      "iam:PutRolePolicy",
      "iam:DeleteRolePolicy",
      "iam:AttachRolePolicy",
      "iam:DetachRolePolicy",
      "iam:CreatePolicy",
      "iam:DeletePolicy",
      "iam:CreatePolicyVersion",
      "iam:DeletePolicyVersion",
      "iam:TagRole",
      "iam:UntagRole",
      "iam:TagPolicy",
      "iam:UntagPolicy",
      "iam:Get*",
      "iam:List*",
    ]
    resources = [
      "arn:aws:iam::${var.aws_account_id}:role/${var.resource_prefix}*",
      "arn:aws:iam::${var.aws_account_id}:policy/${var.resource_prefix}*",
      "arn:aws:iam::${var.aws_account_id}:oidc-provider/token.actions.githubusercontent.com",
    ]
  }
}

resource "aws_iam_role_policy" "deploy" {
  name   = "${var.deploy_role_name}-apply"
  role   = aws_iam_role.deploy.id
  policy = data.aws_iam_policy_document.deploy_permissions.json
}
