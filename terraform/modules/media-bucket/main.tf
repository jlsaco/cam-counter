# Módulo `media-bucket` — bucket S3 de MEDIA del producto (clips/gifs/snapshots).
#
# Es uno de los TRES buckets distintos del producto, JAMÁS conflado con los otros:
#   - cam-counter-rpi-artifacts-950639281773  (backup de binarios de ops, EXISTENTE; NO TOCAR)
#   - cam-counter-media-950639281773          (ESTE bucket; media del producto)
#   - cam-counter-fleet-releases-950639281773 (OTA + manifiestos; lo crea PR11)
#
# Convención de claves de media:
#   media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.{ext}
#
# Seguridad (bucket NUEVO): privado, BlockPublicAccess con las 4 flags en true, SSE-S3
# (AES256), Object Ownership BucketOwnerEnforced (ACLs deshabilitadas) y política de bucket
# TLS-only (deny a peticiones con aws:SecureTransport=false).
#
# F3 — TAGS: los `default_tags` capitalizados {Project, ManagedBy=terraform, Env} MÁS los
# lógicos en minúscula {project=cam-counter, managed_by=mad-runner} se heredan del provider
# de la raíz (S3 distingue mayúsculas, así que el esquema dual-case es válido). Además se
# mergea `local.tags` en el bucket para GARANTIZAR la clave minúscula `managed_by=mad-runner`
# aunque cambiaran los default_tags.

locals {
  # Tags lógicos minúscula (F3) garantizados en el bucket.
  tags = merge(
    {
      project    = "cam-counter"
      managed_by = "mad-runner"
    },
    var.tags,
  )
}

# ───────────────────────── Bucket S3 de media ─────────────────────────
resource "aws_s3_bucket" "media" {
  bucket = var.bucket_name

  tags = local.tags
}

# Versioning (recomendado ON): protege ante sobreescritura/borrado accidental de clips.
resource "aws_s3_bucket_versioning" "media" {
  bucket = aws_s3_bucket.media.id
  versioning_configuration {
    status = var.enable_versioning ? "Enabled" : "Suspended"
  }
}

# Cifrado en reposo SSE-S3 (AES256).
resource "aws_s3_bucket_server_side_encryption_configuration" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Bloqueo total de acceso público (las 4 flags en true).
resource "aws_s3_bucket_public_access_block" "media" {
  bucket                  = aws_s3_bucket.media.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Object Ownership BucketOwnerEnforced: deshabilita ACLs; el dueño del bucket es dueño de
# todos los objetos (los clips que sube el Pi vía PutObject).
resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Ciclo de vida: transición a STANDARD_IA a los 30 días (media fría más barata), expiración
# a los 365 días (retención del histórico de clips) y aborto de multipart incompletos a los
# 7 días (limpieza de subidas a medias).
resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  # Versioning debe estar configurado antes de aplicar reglas de ciclo de vida.
  depends_on = [aws_s3_bucket_versioning.media]

  rule {
    id     = "media-lifecycle"
    status = "Enabled"

    filter {} # aplica a todo el bucket

    transition {
      days          = var.transition_ia_days
      storage_class = "STANDARD_IA"
    }

    expiration {
      days = var.expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = var.abort_multipart_days
    }
  }
}

# Política del bucket: DENIEGA cualquier petición no cifrada (TLS-only).
data "aws_iam_policy_document" "media_tls_only" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.media.arn,
      "${aws_s3_bucket.media.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "media_tls_only" {
  bucket = aws_s3_bucket.media.id
  policy = data.aws_iam_policy_document.media_tls_only.json

  # El public_access_block (block_public_policy) debe estar en su sitio antes de poner una
  # bucket policy, para evitar carreras durante el apply.
  depends_on = [aws_s3_bucket_public_access_block.media]
}
