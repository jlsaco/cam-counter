# Módulo `fleet-releases` — bucket S3 de ARTEFACTOS OTA + MANIFIESTOS DE CANAL.
#
# Es el TERCERO de los tres buckets distintos del producto, JAMÁS conflado con los otros:
#   - cam-counter-rpi-artifacts-950639281773  (backup de binarios de ops, EXISTENTE; NO TOCAR)
#   - cam-counter-media-950639281773          (media del producto; lo creó PR04)
#   - cam-counter-fleet-releases-950639281773 (ESTE bucket; OTA + manifiestos de canal)
#
# Lo CREA y lo APLICA AUTÓNOMAMENTE el RUNNER MAD en PR11 (F2), compartiendo el state remoto
# de `environments/prod` (lock DynamoDB de PR02). El apply es ADITIVO y MONÓTONO (F1): este
# módulo SÓLO añade el bucket de releases; nunca toca recursos de PR02–PR04.
#
# Convención de claves:
#   releases/<version>/cam-counter-edge-<version>-arm64.tar.gz         (artefacto)
#   releases/<version>/cam-counter-edge-<version>-arm64.tar.gz.sha256  (digest)
#   releases/<version>/cam-counter-edge-<version>-arm64.tar.gz.minisig (firma minisign)
#   channels/<channel>/manifest.json                                  (manifiesto del canal)
#   native/<...>                                                      (native_blob fuera del tarball)
#
# Seguridad (bucket NUEVO): privado, BlockPublicAccess con las 4 flags en true, SSE-S3
# (AES256), Object Ownership BucketOwnerEnforced (ACLs deshabilitadas) y política de bucket
# TLS-only (deny a peticiones con aws:SecureTransport=false). El lector es el update-agent
# vía SigV4/IAM (NUNCA presigned). Los escritores son los workflows release/promote con el
# rol de deploy gated por Environment (regla single-writer del manifiesto, ETag If-Match).
#
# Versionado ON: protege el manifiesto del canal (single-writer + If-Match) ante
# sobreescritura accidental y permite auditar el histórico de publicaciones. NO se expira el
# contenido (las releases deben sobrevivir a una Pi mucho tiempo offline); sólo se abortan
# multipart incompletos.
#
# F3 — TAGS: los `default_tags` capitalizados {Project, ManagedBy=terraform, Env} MÁS los
# lógicos en minúscula {project=cam-counter, managed_by=mad-runner} se heredan del provider
# de la raíz (S3 distingue mayúsculas, así que el esquema dual-case es válido). Además se
# mergea `local.tags` en el bucket para GARANTIZAR la clave minúscula `managed_by=mad-runner`
# aunque cambiaran los default_tags. NUNCA `ManagedBy="mad-runner"` capitalizado.

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

# ───────────────────────── Bucket S3 de releases OTA ─────────────────────────
resource "aws_s3_bucket" "releases" {
  bucket = var.bucket_name

  tags = local.tags
}

# Versioning ON: el manifiesto del canal es single-writer con If-Match; versionar protege
# ante sobreescritura/borrado accidental y deja histórico auditable de publicaciones.
resource "aws_s3_bucket_versioning" "releases" {
  bucket = aws_s3_bucket.releases.id
  versioning_configuration {
    status = var.enable_versioning ? "Enabled" : "Suspended"
  }
}

# Cifrado en reposo SSE-S3 (AES256).
resource "aws_s3_bucket_server_side_encryption_configuration" "releases" {
  bucket = aws_s3_bucket.releases.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Bloqueo total de acceso público (las 4 flags en true).
resource "aws_s3_bucket_public_access_block" "releases" {
  bucket                  = aws_s3_bucket.releases.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Object Ownership BucketOwnerEnforced: deshabilita ACLs; el dueño del bucket es dueño de
# todos los objetos (artefactos/manifiestos que publican los workflows de release/promote).
resource "aws_s3_bucket_ownership_controls" "releases" {
  bucket = aws_s3_bucket.releases.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

# Ciclo de vida: NO se expira el contenido (una Pi mucho tiempo offline debe poder seguir
# resolviendo la versión current de su canal). Sólo se abortan los multipart incompletos
# (limpieza de subidas a medias).
resource "aws_s3_bucket_lifecycle_configuration" "releases" {
  bucket = aws_s3_bucket.releases.id

  # Versioning debe estar configurado antes de aplicar reglas de ciclo de vida.
  depends_on = [aws_s3_bucket_versioning.releases]

  rule {
    id     = "abort-incomplete-multipart"
    status = "Enabled"

    filter {} # aplica a todo el bucket

    abort_incomplete_multipart_upload {
      days_after_initiation = var.abort_multipart_days
    }
  }
}

# Política del bucket: DENIEGA cualquier petición no cifrada (TLS-only).
data "aws_iam_policy_document" "releases_tls_only" {
  statement {
    sid    = "DenyInsecureTransport"
    effect = "Deny"

    principals {
      type        = "*"
      identifiers = ["*"]
    }

    actions = ["s3:*"]

    resources = [
      aws_s3_bucket.releases.arn,
      "${aws_s3_bucket.releases.arn}/*",
    ]

    condition {
      test     = "Bool"
      variable = "aws:SecureTransport"
      values   = ["false"]
    }
  }
}

resource "aws_s3_bucket_policy" "releases_tls_only" {
  bucket = aws_s3_bucket.releases.id
  policy = data.aws_iam_policy_document.releases_tls_only.json

  # El public_access_block (block_public_policy) debe estar en su sitio antes de poner una
  # bucket policy, para evitar carreras durante el apply.
  depends_on = [aws_s3_bucket_public_access_block.releases]
}
