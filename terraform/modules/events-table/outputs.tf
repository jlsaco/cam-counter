output "table_name" {
  description = "Nombre de la tabla DynamoDB de eventos (output canónico `events_table_name`, consumido por PR10)."
  value       = aws_dynamodb_table.events.name
}

output "table_arn" {
  description = "ARN de la tabla de eventos (lo consume iam-edge para acotar dynamodb:PutItem)."
  value       = aws_dynamodb_table.events.arn
}

output "gsi1_name" {
  description = "Nombre del GSI1 (por sitio) de la tabla de eventos."
  value       = var.gsi1_name
}
