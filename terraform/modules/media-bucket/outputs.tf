# Outputs del módulo `media-bucket`. La raíz live los reexporta.

output "bucket_name" {
  description = "Nombre del bucket S3 de media del producto."
  value       = aws_s3_bucket.media.id
}

output "bucket_arn" {
  description = "ARN del bucket S3 de media del producto."
  value       = aws_s3_bucket.media.arn
}
