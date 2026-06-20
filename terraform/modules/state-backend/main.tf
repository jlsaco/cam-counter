# ─────────────────────────────────────────────────────────────────────────────
# Backend de estado remoto de Terraform: bucket S3 (.tfstate) + tabla DynamoDB de
# lock de concurrencia. Es un bucket DEDICADO al state; NO almacena media ni
# artefactos OTA (esos son cam-counter-media-... y cam-counter-fleet-releases-...,
# creados en PRs posteriores).
#
# F3 — Tags: los tags de trazabilidad `project = "cam-counter"` y
# `managed_by = "mad-runner"` (MINÚSCULA) + los capitalizados
# `{ Project, ManagedBy = "terraform", Env }` se aplican a TODOS los recursos vía
# `default_tags` del provider de la raíz (terraform/environments/prod/providers.tf).
# Aquí sólo añadimos un `Name` por recurso. NUNCA se usa `ManagedBy = "mad-runner"`.
# ─────────────────────────────────────────────────────────────────────────────

# Bucket S3 que guarda el estado remoto de Terraform.
resource "aws_s3_bucket" "tfstate" {
  bucket = var.state_bucket_name

  tags = {
    Name = var.state_bucket_name
  }
}

# Versionado: permite recuperar revisiones anteriores del .tfstate.
resource "aws_s3_bucket_versioning" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Cifrado en reposo SSE-S3 (AES256).
resource "aws_s3_bucket_server_side_encryption_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

# Bloqueo total de acceso público: las 4 flags en true.
resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Object Ownership: desactiva ACLs (bucket-owner-enforced).
resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Ciclo de vida: expira versiones NO-actuales del state y aborta multipart incompletos.
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  # El versionado debe estar activo antes de gestionar versiones no-actuales.
  depends_on = [aws_s3_bucket_versioning.tfstate]

  rule {
    id     = "expire-noncurrent-state-versions"
    status = "Enabled"

    # filter vacío = la regla aplica a todos los objetos del bucket.
    filter {}

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Política TLS-only: DENIEGA cualquier petición no cifrada (aws:SecureTransport=false).
data "aws_iam_policy_document" "tfstate_tls_only" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.tfstate.arn,
      "${aws_s3_bucket.tfstate.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  policy = data.aws_iam_policy_document.tfstate_tls_only.json

  # Aplica la policy después del Block Public Access para evitar carreras.
  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}

# Tabla DynamoDB de lock de concurrencia de Terraform.
# hash_key EXACTAMENTE "LockID" (lo que Terraform espera para el lock S3).
resource "aws_dynamodb_table" "lock" {
  name         = var.lock_table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"

  attribute {
    name = "LockID"
    type = "S"
  }

  point_in_time_recovery {
    enabled = true
  }

  tags = {
    Name = var.lock_table_name
  }
}
