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

# ───── PR03 — OIDC provider + roles GHA plan/deploy ─────

output "oidc_provider_arn" {
  description = "ARN del proveedor OIDC de GitHub Actions."
  value       = module.iam_github_oidc.oidc_provider_arn
}

output "gha_plan_role_arn" {
  description = "ARN del rol IAM de SOLO LECTURA (plan) asumible por CI vía OIDC."
  value       = module.iam_github_oidc.plan_role_arn
}

output "gha_deploy_role_arn" {
  description = "ARN del rol IAM de apply (uso operativo futuro), gated; nunca pull_request."
  value       = module.iam_github_oidc.deploy_role_arn
}
