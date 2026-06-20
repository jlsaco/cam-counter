output "bucket_name" {
  description = "Nombre del bucket S3 de media del producto."
  value       = aws_s3_bucket.media.id
}

output "bucket_arn" {
  description = "ARN del bucket S3 de media (lo consume iam-edge para acotar s3:PutObject por prefijo)."
  value       = aws_s3_bucket.media.arn
}
