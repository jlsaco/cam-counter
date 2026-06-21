# Outputs del módulo `iam-edge`. La raíz live los reexporta.

output "role_arn" {
  description = "ARN REAL y resoluble del rol per-Pi (lo asume PR10 vía el runner_principal_arn del trust)."
  value       = aws_iam_role.edge.arn
}

output "role_name" {
  description = "Nombre del rol per-Pi."
  value       = aws_iam_role.edge.name
}

output "policy_arn" {
  description = "ARN de la política managed least-privilege adjunta al rol per-Pi."
  value       = aws_iam_policy.edge.arn
}
