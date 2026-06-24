# Módulo `iam-lambda` — rol de ejecución + política inline LEAST-PRIVILEGE por función Lambda.
#
# UN ROL POR FUNCIÓN (events-ingest, devices-register, line-publish, clip-presign, fleet-api):
# nunca compartido, y DISTINTO del rol de borde `cam-counter-edge-{site}-{device}` (iam-edge).
# En vez de duplicar HCL por función, este módulo se INSTANCIA una vez por Lambda con sus ARNs
# acotados. Sigue el patrón de `iam-edge` / `iam-github-oidc` y el canon de `docs/naming-standard.md`.
#
# NAMING (gate de coherencia HCL↔doc, naming-standard.md §5/§11):
#   Lambda   = cam-counter-{function_short_name}            (p. ej. cam-counter-events-ingest)
#   Rol      = cam-counter-{function_short_name}-role       (p. ej. cam-counter-events-ingest-role)
#   Política = cam-counter-{function_short_name}-policy     (inline, mismo nombre lógico)
#   El `function_short_name` es el slug `{dominio}-{accion}` (dominio primero). El issue WP03
#   esbozaba `cam-counter-lambda-{short}-role`; se RECONCILIA al patrón SIN infijo `-lambda-`
#   de naming-standard.md §5 (`cam-counter-{dominio}-{accion}-role`), que es la fuente de verdad
#   del gate. Ver README §"Reconciliación de naming".
#
# LEAST-PRIVILEGE / OPT-IN: el rol sólo recibe permisos de CloudWatch Logs por defecto. Cada
# acceso adicional (DynamoDB / S3 / SQS / X-Ray) es OPT-IN: una variable vacía OMITE por
# completo su statement. Sin `Scan`/`DeleteItem` por defecto en DynamoDB; sin `PutObject`/
# `DeleteObject` por defecto en S3. Nunca referencia los buckets fleet-releases / tfstate /
# rpi-artifacts (no son recursos de plano de datos de estas Lambdas).
#
# F3 — TAGS y CASE-INSENSITIVE de AWS IAM: este módulo crea SÓLO un recurso IAM con tags (el
# rol; la política es inline y no se taggea). La raíz lo instancia con el proveedor IAM-safe
# `aws.iam` (providers = { aws = aws.iam }), cuyos default_tags { Env, project=cam-counter,
# managed_by=mad-runner } NO incluyen las capitalizadas que colisionarían (Project/ManagedBy).
# `local.tags` garantiza además la clave minúscula `managed_by=mad-runner` en el rol.

locals {
  # Tags lógicos minúscula (F3) garantizados en el rol del módulo.
  tags = merge(
    {
      project    = "cam-counter"
      managed_by = "mad-runner"
    },
    var.tags,
  )

  # Naming canónico derivado del slug {dominio}-{accion} (gate HCL↔doc).
  function_name = "${var.name_prefix}-${var.function_short_name}"
  role_name     = "${local.function_name}-role"
  policy_name   = "${local.function_name}-policy"

  # ARN acotado del log group de la función: /aws/lambda/cam-counter-{function_short_name}.
  # El sufijo `:*` cubre los log streams creados dentro del grupo.
  log_group_arn = "arn:aws:logs:${var.aws_region}:${var.aws_account_id}:log-group:/aws/lambda/${local.function_name}:*"

  # Flags de opt-in: una variable vacía OMITE por completo el statement correspondiente.
  enable_dynamodb = length(var.dynamodb_table_arns) > 0
  enable_s3       = var.s3_bucket_arn != ""
  enable_sqs      = var.sqs_dlq_arn != ""

  # Recursos del statement de DynamoDB: tablas + índices (un Query sobre un GSI exige su ARN).
  dynamodb_resources = concat(var.dynamodb_table_arns, var.dynamodb_gsi_arns)
}

# ───────────────────────── Trust policy: sólo el servicio Lambda ─────────────────────────
#
# El rol lo asume EXCLUSIVAMENTE el servicio Lambda (`lambda.amazonaws.com`). Least-privilege:
# ningún otro principal puede asumirlo.
data "aws_iam_policy_document" "lambda_trust" {
  statement {
    sid     = "LambdaServiceAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["lambda.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "lambda" {
  name                 = local.role_name
  description          = "Rol de ejecución least-privilege de la Lambda ${local.function_name}: logs propios + accesos acotados opt-in."
  assume_role_policy   = data.aws_iam_policy_document.lambda_trust.json
  max_session_duration = 3600

  tags = local.tags
}

# ───────────────────────── Política inline least-privilege de la función ─────────────────────────
data "aws_iam_policy_document" "lambda_permissions" {
  # (1) CloudWatch Logs — SIEMPRE presente. Acotado al log group PROPIO de la función
  #     (`/aws/lambda/cam-counter-{name}`): la Lambda no escribe en logs de otras funciones.
  statement {
    sid    = "OwnLogGroupOnly"
    effect = "Allow"
    actions = [
      "logs:CreateLogGroup",
      "logs:CreateLogStream",
      "logs:PutLogEvents",
    ]
    resources = [local.log_group_arn]
  }

  # (2) DynamoDB — OPT-IN. Sólo si `dynamodb_table_arns` no está vacío. Acciones acotadas a
  #     `dynamodb_actions` (default PutItem/UpdateItem; sin Scan/DeleteItem) y recursos a las
  #     tablas + índices EXACTOS (nunca `*`).
  dynamic "statement" {
    for_each = local.enable_dynamodb ? [1] : []
    content {
      sid       = "DynamoDBScopedAccess"
      effect    = "Allow"
      actions   = var.dynamodb_actions
      resources = local.dynamodb_resources
    }
  }

  # (3) S3 — OPT-IN. Sólo si `s3_bucket_arn` no vacío. Acotado al prefijo `s3_prefix` (default
  #     `media/*`) y a `s3_actions` (default GetObject; sin PutObject/Delete). Exige TLS
  #     (`aws:SecureTransport = true`): se DENIEGA cualquier petición no cifrada.
  dynamic "statement" {
    for_each = local.enable_s3 ? [1] : []
    content {
      sid       = "S3ScopedPrefixTlsOnly"
      effect    = "Allow"
      actions   = var.s3_actions
      resources = ["${var.s3_bucket_arn}/${var.s3_prefix}"]

      condition {
        test     = "Bool"
        variable = "aws:SecureTransport"
        values   = ["true"]
      }
    }
  }

  # (4) SQS DLQ — OPT-IN. Sólo si `sqs_dlq_arn` no vacío. Únicamente `sqs:SendMessage` sobre la
  #     cola indicada (lo justo para depositar invocaciones fallidas en la DLQ).
  dynamic "statement" {
    for_each = local.enable_sqs ? [1] : []
    content {
      sid       = "DlqSendMessageOnly"
      effect    = "Allow"
      actions   = ["sqs:SendMessage"]
      resources = [var.sqs_dlq_arn]
    }
  }

  # (5) X-Ray — OPT-IN por flag. Las acciones de X-Ray no admiten permisos a nivel de recurso,
  #     por lo que su recurso es `*` (acotación inherente del servicio).
  dynamic "statement" {
    for_each = var.enable_xray ? [1] : []
    content {
      sid    = "XRayActiveTracing"
      effect = "Allow"
      actions = [
        "xray:PutTraceSegments",
        "xray:PutTelemetryRecords",
      ]
      resources = ["*"]
    }
  }

  # (6) Statements extra — escotilla controlada y revisable (default []). Cada uno acota sus
  #     propios `resources`; este módulo no fuerza wildcards.
  dynamic "statement" {
    for_each = var.extra_policy_statements
    content {
      sid       = statement.value.sid
      effect    = statement.value.effect
      actions   = statement.value.actions
      resources = statement.value.resources
    }
  }
}

# Política INLINE (no managed): vive y muere con el rol de la función. Un rol por función →
# una política inline por función, sin políticas managed compartidas entre Lambdas.
resource "aws_iam_role_policy" "lambda" {
  name   = local.policy_name
  role   = aws_iam_role.lambda.id
  policy = data.aws_iam_policy_document.lambda_permissions.json
}
