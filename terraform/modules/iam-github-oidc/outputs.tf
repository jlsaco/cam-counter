# Outputs del módulo `iam-github-oidc`. La raíz live los reexporta.

output "oidc_provider_arn" {
  description = "ARN del proveedor OIDC de GitHub Actions (token.actions.githubusercontent.com)."
  value       = local.oidc_provider_arn
}

output "plan_role_arn" {
  description = "ARN del rol IAM de PLAN (CI plan-only, solo lectura)."
  value       = aws_iam_role.plan.arn
}

output "deploy_role_arn" {
  description = "ARN del rol IAM de DEPLOY (apply gated; uso operativo futuro)."
  value       = aws_iam_role.deploy.arn
}
