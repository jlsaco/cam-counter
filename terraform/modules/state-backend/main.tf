# Módulo `state-backend` — backend de estado remoto de Terraform.
#
# Crea EXCLUSIVAMENTE el bucket S3 del .tfstate y la tabla DynamoDB de lock que usa
# toda la pila de infra (`terraform/environments/prod`). NO almacena media ni
# artefactos OTA (ésos son buckets distintos creados en PR04/PR11).
#
# Los tags de trazabilidad (project=cam-counter, managed_by=mad-runner en MINÚSCULA y
# {Project, ManagedBy=terraform, Env} CAPITALIZADOS) se heredan vía `default_tags` del
# provider declarado en la raíz live (ver F3 en CLAUDE.md). El módulo no los repite.

# ───────────────────────── Bucket S3 del .tfstate ─────────────────────────
resource "aws_s3_bucket" "tfstate" {
  bucket = var.state_bucket_name
}

# Versioning ON: conservamos el historial del .tfstate para poder recuperar ante
# corrupción o un apply erróneo.
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
    bucket_key_enabled = true
  }
}

# Bloqueo total de acceso público (las 4 flags en true).
resource "aws_s3_bucket_public_access_block" "tfstate" {
  bucket                  = aws_s3_bucket.tfstate.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Object Ownership BucketOwnerEnforced: deshabilita ACLs; el dueño del bucket es dueño
# de todos los objetos.
resource "aws_s3_bucket_ownership_controls" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Ciclo de vida: expira las versiones NO actuales del state (limpieza de historial) y
# aborta multipart uploads incompletos.
resource "aws_s3_bucket_lifecycle_configuration" "tfstate" {
  bucket = aws_s3_bucket.tfstate.id

  # Versioning debe estar configurado antes de aplicar reglas que dependen de versiones.
  depends_on = [aws_s3_bucket_versioning.tfstate]

  rule {
    id     = "expire-noncurrent-tfstate-versions"
    status = "Enabled"

    filter {} # aplica a todo el bucket

    noncurrent_version_expiration {
      noncurrent_days = var.noncurrent_version_expiration_days
    }

    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

# Política del bucket: DENIEGA cualquier petición no cifrada (TLS-only).
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

resource "aws_s3_bucket_policy" "tfstate_tls_only" {
  bucket = aws_s3_bucket.tfstate.id
  policy = data.aws_iam_policy_document.tfstate_tls_only.json

  # El public_access_block (block_public_policy) debe estar en su sitio antes de poner
  # una bucket policy, para evitar carreras durante el apply.
  depends_on = [aws_s3_bucket_public_access_block.tfstate]
}

# ──────────────────────── Tabla DynamoDB de lock ────────────────────────
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
}
