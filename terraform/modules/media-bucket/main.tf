# ─────────────────────────────────────────────────────────────────────────────
# Bucket S3 de MEDIA del producto (clips / gifs / snapshots de eventos de cruce).
#
# Mismas garantías de seguridad que el resto de buckets NUEVOS de la iniciativa
# (ver state-backend y CLAUDE.md §7):
#   - privado, Block Public Access con las 4 flags en true,
#   - cifrado en reposo SSE-S3 (AES256),
#   - Object Ownership BucketOwnerEnforced (ACLs deshabilitadas),
#   - bucket policy TLS-only (deny a aws:SecureTransport=false).
# MÁS, específico de media:
#   - versionado (recuperar clips sobrescritos/borrados),
#   - lifecycle: IA@30d → expire@365d, abort multipart incompleto @7d.
#
# El Pi sube clips con s3:PutObject restringido por prefijo
# media/{site_id}/{device_id}/* (ver módulo iam-edge). NO almacena state ni
# artefactos OTA. F3 — tags lógicos minúscula vía var.tags + default_tags raíz.
# ─────────────────────────────────────────────────────────────────────────────

resource "aws_s3_bucket" "media" {
  bucket = var.bucket_name

  tags = merge(var.tags, {
    Name = var.bucket_name
  })
}

# Versionado: permite recuperar clips sobrescritos o borrados accidentalmente.
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

# Bloqueo total de acceso público: las 4 flags en true.
resource "aws_s3_bucket_public_access_block" "media" {
  bucket = aws_s3_bucket.media.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Object Ownership: desactiva ACLs (bucket-owner-enforced).
resource "aws_s3_bucket_ownership_controls" "media" {
  bucket = aws_s3_bucket.media.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Ciclo de vida de la media: transición a IA, expiración y limpieza de multipart.
resource "aws_s3_bucket_lifecycle_configuration" "media" {
  bucket = aws_s3_bucket.media.id

  # El versionado debe estar configurado antes de gestionar el lifecycle.
  depends_on = [aws_s3_bucket_versioning.media]

  rule {
    id     = "media-transition-ia-expire"
    status = "Enabled"

    # filter vacío = la regla aplica a todos los objetos del bucket.
    filter {}

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

# Política TLS-only: DENIEGA cualquier petición no cifrada (aws:SecureTransport=false).
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

resource "aws_s3_bucket_policy" "media" {
  bucket = aws_s3_bucket.media.id
  policy = data.aws_iam_policy_document.media_tls_only.json

  # Aplica la policy después del Block Public Access para evitar carreras.
  depends_on = [aws_s3_bucket_public_access_block.media]
}
