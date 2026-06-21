# Outputs del módulo `fleet-releases`. La raíz live los reexporta.

output "bucket_name" {
  description = "Nombre del bucket S3 de releases OTA + manifiestos de canal."
  value       = aws_s3_bucket.releases.id
}

output "bucket_arn" {
  description = "ARN del bucket S3 de releases OTA + manifiestos de canal."
  value       = aws_s3_bucket.releases.arn
}
