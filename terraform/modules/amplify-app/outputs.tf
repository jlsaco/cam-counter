# Outputs del módulo `amplify-app`. Los consume la raíz live para la RECONCILIACIÓN del
# dominio real (segundo apply, update IN-PLACE):
#   - `production_url` → `module.cognito.callback_urls` / `logout_urls` (app client web).
#   - `production_url` → `module.api_dashboard.cors_allow_origins` (CORS de la HTTP API).
# El `app_id` / `default_domain` no se conocen hasta el PRIMER apply (Amplify los genera), por
# eso la reconciliación es un segundo apply var-driven (ver README §"Reconciliación de dominio").

output "app_id" {
  description = "ID de la Amplify App (p. ej. d1a2b3c4d5e6f7). Forma parte del dominio por defecto."
  value       = aws_amplify_app.console.id
}

output "app_arn" {
  description = "ARN de la Amplify App."
  value       = aws_amplify_app.console.arn
}

output "default_domain" {
  description = "Dominio por defecto de la app (`<app_id>.amplifyapp.com`). El sitio se sirve en `<branch>.<default_domain>`."
  value       = aws_amplify_app.console.default_domain
}

output "branch_name" {
  description = "Nombre del branch de producción servido (`main`)."
  value       = aws_amplify_branch.main.branch_name
}

output "production_url" {
  description = <<-EOT
    URL HTTPS pública del sitio en el dominio por defecto de Amplify
    (`https://<branch>.<app_id>.amplifyapp.com`). Es el ORIGEN real que se reconcilia en
    Cognito (callback/logout) y en el CORS de la API (segundo apply, update in-place).
  EOT
  value       = "https://${aws_amplify_branch.main.branch_name}.${aws_amplify_app.console.default_domain}"
}

output "custom_domain_url" {
  description = "URL del dominio personalizado si se definió `custom_domain`; cadena vacía en caso contrario."
  value       = var.custom_domain != "" ? "https://${var.custom_domain}" : ""
}
