output "state_bucket_name" {
  description = "Nombre del bucket S3 del estado remoto."
  value       = aws_s3_bucket.tfstate.id
}

output "state_bucket_arn" {
  description = "ARN del bucket S3 del estado remoto."
  value       = aws_s3_bucket.tfstate.arn
}

output "lock_table_name" {
  description = "Nombre de la tabla DynamoDB de lock."
  value       = aws_dynamodb_table.lock.name
}

output "lock_table_arn" {
  description = "ARN de la tabla DynamoDB de lock."
  value       = aws_dynamodb_table.lock.arn
}
