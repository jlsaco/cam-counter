# Reexporta los outputs del módulo `state-backend` para inspección y para que PRs
# posteriores de la pila puedan referenciarlos.

output "state_bucket_name" {
  description = "Nombre del bucket S3 del .tfstate remoto."
  value       = module.state_backend.state_bucket_name
}

output "state_bucket_arn" {
  description = "ARN del bucket S3 del .tfstate remoto."
  value       = module.state_backend.state_bucket_arn
}

output "lock_table_name" {
  description = "Nombre de la tabla DynamoDB de lock."
  value       = module.state_backend.lock_table_name
}

output "lock_table_arn" {
  description = "ARN de la tabla DynamoDB de lock."
  value       = module.state_backend.lock_table_arn
}
