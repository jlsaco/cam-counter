# Outputs del módulo `observability`.

output "alarms_topic_arn" {
  description = "ARN del topic SNS al que apuntan todas las alarmas (alarm_actions / ok_actions)."
  value       = aws_sns_topic.alarms.arn
}

output "dashboard_name" {
  description = "Nombre del dashboard de CloudWatch de la flota."
  value       = aws_cloudwatch_dashboard.fleet.dashboard_name
}

output "device_status_table_name" {
  description = "Nombre de la tabla DynamoDB de status de presencia (LWT backstop)."
  value       = aws_dynamodb_table.device_status.name
}

output "device_status_table_arn" {
  description = "ARN de la tabla DynamoDB de status de presencia."
  value       = aws_dynamodb_table.device_status.arn
}

output "presence_rule_name" {
  description = "Nombre de la IoT Topic Rule que enruta los Lifecycle Events de desconexión."
  value       = aws_iot_topic_rule.presence_disconnected.name
}

output "iot_status_role_arn" {
  description = "ARN del rol que asume la IoT Rule de presencia para escribir en la tabla de status."
  value       = aws_iam_role.iot_status.arn
}
