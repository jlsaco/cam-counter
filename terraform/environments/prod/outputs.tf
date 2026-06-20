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

# ───────────────────────── PR03 — IAM GitHub OIDC ─────────────────────────

output "oidc_provider_arn" {
  description = "ARN del proveedor OIDC de GitHub Actions (token.actions.githubusercontent.com)."
  value       = module.iam_github_oidc.oidc_provider_arn
}

output "gha_plan_role_arn" {
  description = "ARN del rol IAM de PLAN (CI plan-only, solo lectura)."
  value       = module.iam_github_oidc.plan_role_arn
}

output "gha_deploy_role_arn" {
  description = "ARN del rol IAM de DEPLOY (apply gated; uso operativo futuro)."
  value       = module.iam_github_oidc.deploy_role_arn
}
