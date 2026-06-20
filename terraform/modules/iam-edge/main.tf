# Módulo `iam-edge` — rol + política IAM LEAST-PRIVILEGE por Pi.
#
# Concede al Pi EXACTAMENTE lo que necesita, acotado por `site_id`/`device_id`:
#   - S3 media:    s3:PutObject (+ AbortMultipartUpload, + GetObject para reintentos) SÓLO en
#                  media/${site_id}/${device_id}/* del bucket de media.
#   - S3 releases: s3:GetObject (lectura SigV4 del agente OTA) en releases/* y channels/* del
#                  bucket de releases; s3:ListBucket acotado por s3:prefix. Nunca presigned.
#   - DynamoDB events:  dynamodb:PutItem acotado por dynamodb:LeadingKeys al prefijo del Pi.
#   - DynamoDB devices: dynamodb:GetItem/UpdateItem SÓLO sobre la propia fila DEVICE#${device_id}.
#
# TRUST (F7 — contrato estable para PR10): el rol lo asume el `runner_principal_arn` ESTABLE
# (ARN del rol/usuario base del runner, normalizado de assumed-role→role) para que PR10 lo
# asuma y valide el least-privilege. En producción el Pi recibe credenciales STS de CORTA VIDA
# (IAM Roles Anywhere es el hook de provisioning para v1.1; ver README).
#
# F3 — TAGS y CASE-INSENSITIVE de AWS IAM: este módulo crea SÓLO recursos IAM (rol + política),
# cuyas claves de tag son CASE-INSENSITIVE. La raíz lo instancia con el proveedor IAM-safe
# `aws.iam` (providers = { aws = aws.iam }), cuyos default_tags { Env, project=cam-counter,
# managed_by=mad-runner } NO incluyen las capitalizadas que colisionarían (Project/ManagedBy).
# `local.tags` garantiza además la clave minúscula `managed_by=mad-runner` en rol y política.

locals {
  # Tags lógicos minúscula (F3) garantizados en TODOS los recursos del módulo.
  tags = merge(
    {
      project    = "cam-counter"
      managed_by = "mad-runner"
    },
    var.tags,
  )

  role_name   = "${var.name_prefix}-${var.site_id}-${var.device_id}"
  policy_name = "${var.name_prefix}-${var.site_id}-${var.device_id}-policy"

  # ARN del bucket de releases (lo crea PR11; referenciar su ARN NO requiere que exista aún).
  releases_bucket_arn = "arn:aws:s3:::${var.releases_bucket_name}"
}

# ───────────────────────── Trust policy del rol per-Pi (F7) ─────────────────────────
#
# DEBE listar EXPLÍCITAMENTE el `runner_principal_arn`: un trust que no liste al runner hace
# que AssumeRole falle aunque el runner tenga el permiso. Least-privilege: SÓLO el runner.
# (IAM Roles Anywhere se añadirá como principal de servicio en v1.1 — ver README.)
data "aws_iam_policy_document" "edge_trust" {
  statement {
    sid     = "RunnerAssumeForValidation"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "AWS"
      identifiers = [var.runner_principal_arn]
    }
  }
}

resource "aws_iam_role" "edge" {
  name                 = local.role_name
  description          = "Rol least-privilege del Pi ${var.device_id}@${var.site_id}: subir media+eventos, leer releases OTA. Credenciales STS de corta vida."
  assume_role_policy   = data.aws_iam_policy_document.edge_trust.json
  max_session_duration = var.max_session_duration

  tags = local.tags
}

# ───────────────────────── Política least-privilege del Pi ─────────────────────────
data "aws_iam_policy_document" "edge_permissions" {
  # (1) S3 MEDIA — subir clips/gifs/snapshots SÓLO al prefijo del propio Pi.
  #     GetObject incluido para flujos de reintento (re-subida idempotente del mismo objeto).
  statement {
    sid    = "MediaPutOwnPrefixOnly"
    effect = "Allow"
    actions = [
      "s3:PutObject",
      "s3:AbortMultipartUpload",
      "s3:GetObject",
    ]
    resources = [
      "${var.media_bucket_arn}/media/${var.site_id}/${var.device_id}/*",
    ]
  }

  # (2) S3 RELEASES — lectura SigV4 del agente OTA: artefactos y manifiestos de canal.
  statement {
    sid    = "ReleasesReadObjects"
    effect = "Allow"
    actions = [
      "s3:GetObject",
    ]
    resources = [
      "${local.releases_bucket_arn}/releases/*",
      "${local.releases_bucket_arn}/channels/*",
    ]
  }

  # (2b) S3 RELEASES — ListBucket acotado por s3:prefix a releases/ y channels/ (si el agente
  #      lista para descubrir versiones/manifiestos). Nunca presigned URLs.
  statement {
    sid    = "ReleasesListScoped"
    effect = "Allow"
    actions = [
      "s3:ListBucket",
    ]
    resources = [
      local.releases_bucket_arn,
    ]
    condition {
      test     = "StringLike"
      variable = "s3:prefix"
      values = [
        "releases/*",
        "channels/*",
      ]
    }
  }

  # (3) DYNAMODB EVENTS — PutItem acotado por dynamodb:LeadingKeys al prefijo del Pi.
  #     La PK es CAM#{site}#{device}#{camera}: como `camera` varía, se usa StringLike con
  #     wildcard de sufijo `CAM#${site}#${device}#*`. Así el Pi NO puede escribir eventos de
  #     otro device_id (su PutItem a CAM#otro#otro#... no cumple la condición → DENY).
  #     (Si la cuenta/región no honrara el wildcard en LeadingKeys, la alternativa es enumerar
  #     las cámaras del device como lista de LeadingKeys; ver README.)
  statement {
    sid    = "EventsPutOwnDeviceOnly"
    effect = "Allow"
    actions = [
      "dynamodb:PutItem",
    ]
    resources = [
      var.events_table_arn,
    ]
    condition {
      test     = "ForAllValues:StringLike"
      variable = "dynamodb:LeadingKeys"
      values = [
        "CAM#${var.site_id}#${var.device_id}#*",
      ]
    }
  }

  # (4) DYNAMODB DEVICES — Get/UpdateItem SÓLO sobre la propia fila DEVICE#${device_id}
  #     (heartbeat: reported_version/last_seen_at/status). NO PutItem arbitrario, NO filas
  #     de otros dispositivos.
  statement {
    sid    = "DevicesOwnRowOnly"
    effect = "Allow"
    actions = [
      "dynamodb:GetItem",
      "dynamodb:UpdateItem",
    ]
    resources = [
      var.devices_table_arn,
    ]
    condition {
      test     = "ForAllValues:StringEquals"
      variable = "dynamodb:LeadingKeys"
      values = [
        "DEVICE#${var.device_id}",
      ]
    }
  }
}

resource "aws_iam_policy" "edge" {
  name        = local.policy_name
  description = "Least-privilege del Pi ${var.device_id}@${var.site_id}: media own-prefix, eventos own-device, registry own-row, releases read-only."
  policy      = data.aws_iam_policy_document.edge_permissions.json

  tags = local.tags
}

resource "aws_iam_role_policy_attachment" "edge" {
  role       = aws_iam_role.edge.name
  policy_arn = aws_iam_policy.edge.arn
}
