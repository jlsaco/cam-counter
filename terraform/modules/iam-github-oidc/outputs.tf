output "oidc_provider_arn" {
  description = "ARN del proveedor OIDC de GitHub Actions (token.actions.githubusercontent.com)."
  value       = local.oidc_provider_arn
}

output "plan_role_arn" {
  description = "ARN del rol IAM de SOLO LECTURA (plan) asumible por CI vía OIDC desde pull_request/main."
  value       = aws_iam_role.plan.arn
}

output "deploy_role_arn" {
  description = "ARN del rol IAM de apply (uso operativo futuro), gated a environment:prod/main/tags; nunca pull_request."
  value       = aws_iam_role.deploy.arn
}
