# Módulo `iot-credential-provider` — IoT Credentials Provider para el acceso S3 del borde.
#
# QUÉ RESUELVE: el device debe seguir subiendo clips MP4 a S3, pero SIN credenciales AWS
# directas ni un segundo secreto. El device llama al **credentials endpoint** de IoT con su
# MISMO cert mTLS y recibe credenciales STS de CORTA VIDA del rol `cam-counter-edge-s3-role`
# (vía el role alias `cam-counter-edge-s3-role-alias`), acotado a su propio prefijo de media.
# Reusa la identidad del cert: cero llaves estáticas en el dispositivo.
#
# DOS RECURSOS:
#   1. aws_iam_role        `cam-counter-edge-s3-role`        — trust en credentials.iot.amazonaws.com
#   2. aws_iot_role_alias  `cam-counter-edge-s3-role-alias`  — el alias que el cert presenta
#
# NAMING (naming-standard.md §5/§8): el rol y el alias NO llevan el infijo per-Pi `-edge-{site}-{device}`
# (ese es el del rol per-Pi `iam-edge`). El aislamiento multi-tenant en producción se hace por
# ThingName vía variables de política IoT; aquí el prefijo S3 se acota por `site_id`/`device_id`
# del provisioning del PRIMER Pi (placeholders no sensibles), igual que hace `iam-edge`.
#
# LEAST-PRIVILEGE (criterio de aceptación WP04): la política concede SÓLO `s3:PutObject`,
# acotado por **Resource ARN** a `media/{site_id}/{device_id}/*` del bucket de media, y exige
# `aws:SecureTransport = true` (TLS-only). SIN Get/List/Delete. El bucket NO lo gestiona este
# módulo (se referencia por ARN; en la raíz proviene de un `data` source).
#
# F3 — TAGS y CASE-INSENSITIVE de AWS IAM: el rol IAM tiene claves de tag CASE-INSENSITIVE, así
# que la raíz instancia este módulo con el proveedor IAM-safe `aws.iam` (providers = { aws =
# aws.iam }), cuyos default_tags { Env, project=cam-counter, managed_by=mad-runner } NO incluyen
# las capitalizadas que colisionarían (Project/ManagedBy). `local.tags` garantiza además la clave
# minúscula `managed_by=mad-runner` en el rol y el role alias.

locals {
  # Tags lógicos minúscula (F3) garantizados en los recursos del módulo.
  tags = merge(
    {
      project    = "cam-counter"
      managed_by = "mad-runner"
    },
    var.tags,
  )

  # Resource ARN exacto del prefijo de media del Pi (NUNCA `*` de bucket completo).
  # Separador `/` del prefijo S3: por eso se construye desde site_id/device_id del
  # provisioning y NO desde el ThingName `cam-counter-{site}-{device}` (separador `-`).
  media_put_resource = "${var.media_bucket_arn}/media/${var.site_id}/${var.device_id}/*"
}

# ───────────────── Trust policy: SÓLO el IoT Credentials Provider ─────────────────
#
# El rol lo asume EXCLUSIVAMENTE el servicio del credentials endpoint
# (`credentials.iot.amazonaws.com`), en nombre del cert X.509 del Thing. Least-privilege:
# ningún otro principal (ni el runner, ni Lambda, ni el Pi directamente) puede asumirlo.
data "aws_iam_policy_document" "edge_s3_trust" {
  statement {
    sid     = "IoTCredentialProviderAssume"
    effect  = "Allow"
    actions = ["sts:AssumeRole"]

    principals {
      type        = "Service"
      identifiers = ["credentials.iot.amazonaws.com"]
    }
  }
}

resource "aws_iam_role" "edge_s3" {
  name               = var.edge_s3_role_name
  description        = "Rol del IoT Credentials Provider: el cert del Pi obtiene STS de corta vida para s3:PutObject en su propio prefijo de media (TLS-only)."
  assume_role_policy = data.aws_iam_policy_document.edge_s3_trust.json

  # AWS IoT exige `credential_duration <= max_session_duration` del rol. IAM acota
  # max_session_duration al rango [3600, 43200]; el credentials provider permite [900, 43200].
  # `max(3600, ...)` respeta el suelo de IAM y garantiza que nunca sea menor que la duración
  # de la credencial (si credential_duration < 3600, el alias sigue siendo válido).
  max_session_duration = max(3600, var.credential_duration_seconds)

  tags = local.tags

  lifecycle {
    # Cross-check: el ARN del bucket debe corresponder a `media_bucket_name` (evita que un
    # ARN de otro bucket se cuele en el Resource del PutObject).
    precondition {
      condition     = endswith(var.media_bucket_arn, var.media_bucket_name)
      error_message = "media_bucket_arn (${var.media_bucket_arn}) debe corresponder a media_bucket_name (${var.media_bucket_name})."
    }
  }
}

# ───────────────── Política inline least-privilege: SÓLO PutObject, prefijo propio, TLS ─────────────────
#
# NOTA DEL REVISOR (WP04): la spec original usaba `Condition StringLike s3:prefix` sobre
# PutObject — es INERTE (`s3:prefix` sólo aplica a ListBucket). Aquí se acota por **Resource
# ARN** al prefijo real `media/{site_id}/{device_id}/*`. SIN Get/List/Delete.
data "aws_iam_policy_document" "edge_s3_permissions" {
  statement {
    sid       = "MediaPutOwnPrefixTlsOnly"
    effect    = "Allow"
    actions   = ["s3:PutObject"]
    resources = [local.media_put_resource]

    # TLS-only: se DENIEGA cualquier subida no cifrada.
    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["true"]
    }
  }
}

# Política INLINE (no managed): vive y muere con el rol. Un rol de un único propósito → una
# política inline de un único statement.
resource "aws_iam_role_policy" "edge_s3" {
  name   = "${var.edge_s3_role_name}-policy"
  role   = aws_iam_role.edge_s3.id
  policy = data.aws_iam_policy_document.edge_s3_permissions.json
}

# ───────────────── Role alias del IoT Credentials Provider ─────────────────
#
# El device presenta su cert mTLS al credentials endpoint indicando ESTE alias; IoT verifica
# que el cert está activo y adjunto a una policy que permite `iot:AssumeRoleWithCertificate`
# sobre este alias, y devuelve credenciales STS del rol con `credential_duration` de vida.
resource "aws_iot_role_alias" "edge_s3" {
  alias               = var.role_alias_name
  role_arn            = aws_iam_role.edge_s3.arn
  credential_duration = var.credential_duration_seconds

  tags = local.tags
}
