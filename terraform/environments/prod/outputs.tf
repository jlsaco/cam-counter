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

# ───────────────────── PR04 — media bucket + events/devices + IAM per-Pi ─────────────────────
#
# CONTRATO CANÓNICO cross-subsistema. PR10 consume estos outputs vía
# `terraform -chdir=terraform/environments/prod output -raw <name>` para su prueba de
# integración real contra AWS y para asumir el rol per-Pi (F7/F10).

output "media_bucket_name" {
  description = "Nombre del bucket S3 de media del producto (clips/gifs/snapshots). Fuente canónica para PR10."
  value       = module.media_bucket.bucket_name
}

output "media_bucket_arn" {
  description = "ARN del bucket S3 de media del producto."
  value       = module.media_bucket.bucket_arn
}

output "events_table_name" {
  description = "Nombre de la tabla DynamoDB de eventos de cruce. Fuente canónica para PR10."
  value       = module.events_table.table_name
}

output "events_table_arn" {
  description = "ARN de la tabla DynamoDB de eventos de cruce."
  value       = module.events_table.table_arn
}

output "events_gsi1_name" {
  description = "Nombre del GSI1 por sitio de la tabla de eventos (GSI1PK=SITE#..., GSI1SK=TS#...)."
  value       = module.events_table.gsi1_name
}

output "devices_table_name" {
  description = "Nombre de la tabla DynamoDB de registro de dispositivos."
  value       = module.device_registry.table_name
}

output "devices_table_arn" {
  description = "ARN de la tabla DynamoDB de registro de dispositivos."
  value       = module.device_registry.table_arn
}

output "devices_gsi1_name" {
  description = "Nombre del GSI1 por canal de la tabla de dispositivos (GSI1PK=CHANNEL#..., GSI1SK=DEVICE#...)."
  value       = module.device_registry.gsi1_name
}

output "edge_role_arn" {
  description = "ARN REAL y resoluble del rol per-Pi del primer dispositivo. Fuente canónica para PR10 (lo asume vía el runner_principal_arn del trust, F7)."
  value       = module.iam_edge.role_arn
}

output "edge_role_name" {
  description = "Nombre del rol per-Pi del primer dispositivo."
  value       = module.iam_edge.role_name
}

output "edge_policy_arn" {
  description = "ARN de la política managed least-privilege adjunta al rol per-Pi."
  value       = module.iam_edge.policy_arn
}
