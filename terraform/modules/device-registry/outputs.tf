# Outputs del módulo `device-registry`. La raíz live los reexporta.

output "table_name" {
  description = "Nombre de la tabla DynamoDB de registro de dispositivos."
  value       = aws_dynamodb_table.devices.name
}

output "table_arn" {
  description = "ARN de la tabla DynamoDB de registro de dispositivos."
  value       = aws_dynamodb_table.devices.arn
}

output "gsi1_name" {
  description = "Nombre del GSI1 por canal (GSI1PK=CHANNEL#..., GSI1SK=DEVICE#...)."
  value       = var.gsi1_name
}
