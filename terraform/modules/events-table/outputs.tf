# Outputs del módulo `events-table`. La raíz live los reexporta.

output "table_name" {
  description = "Nombre de la tabla DynamoDB de eventos de cruce."
  value       = aws_dynamodb_table.events.name
}

output "table_arn" {
  description = "ARN de la tabla DynamoDB de eventos de cruce."
  value       = aws_dynamodb_table.events.arn
}

output "gsi1_name" {
  description = "Nombre del GSI1 por sitio (GSI1PK=SITE#..., GSI1SK=TS#...)."
  value       = var.gsi1_name
}
