# Outputs del módulo `iam-lambda`. La raíz live los reexporta / los consume la definición de
# la función Lambda (`role` = role_arn).

output "role_arn" {
  description = "ARN REAL y resoluble del rol de ejecución de la función (se asigna a la Lambda)."
  value       = aws_iam_role.lambda.arn
}

output "role_name" {
  description = "Nombre del rol de ejecución de la función (canon cam-counter-{function_short_name}-role)."
  value       = aws_iam_role.lambda.name
}
