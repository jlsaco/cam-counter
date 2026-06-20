output "state_bucket_name" {
  description = "Bucket S3 del estado remoto de Terraform."
  value       = module.state_backend.state_bucket_name
}

output "state_bucket_arn" {
  description = "ARN del bucket S3 del estado remoto."
  value       = module.state_backend.state_bucket_arn
}

output "lock_table_name" {
  description = "Tabla DynamoDB de lock del estado remoto."
  value       = module.state_backend.lock_table_name
}

output "lock_table_arn" {
  description = "ARN de la tabla DynamoDB de lock del estado remoto."
  value       = module.state_backend.lock_table_arn
}
