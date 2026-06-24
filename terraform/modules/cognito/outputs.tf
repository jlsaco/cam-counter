# Outputs del módulo `cognito`. Los consumen:
#   - api-dashboard (WP futuro): `user_pool_id` + `user_pool_arn` para el authorizer Cognito de
#     API Gateway; `web_client_id` para la SPA.
#   - amplify (WP13): `user_pool_id`, `web_client_id`, `hosted_ui_domain` para la config de auth
#     de la consola; el callback real reconcilia `callback_urls` IN-PLACE.
#   - validación WP11: `test_client_id` para `aws cognito-idp admin-initiate-auth` (curl + JWT).

output "user_pool_id" {
  description = "ID del User Pool de operadores de flota (us-east-1_XXXX). Authorizer de API Gateway + config de Amplify."
  value       = aws_cognito_user_pool.fleet.id
}

output "user_pool_arn" {
  description = "ARN del User Pool. Resource del authorizer Cognito de API Gateway (api-dashboard)."
  value       = aws_cognito_user_pool.fleet.arn
}

output "user_pool_endpoint" {
  description = "Endpoint del User Pool (cognito-idp.us-east-1.amazonaws.com/us-east-1_XXXX). Issuer de los JWT / provider del Identity Pool."
  value       = aws_cognito_user_pool.fleet.endpoint
}

output "hosted_ui_domain" {
  description = "Domain de la Hosted UI de Cognito (prefijo). FQDN: <domain>.auth.us-east-1.amazoncognito.com."
  value       = aws_cognito_user_pool_domain.fleet.domain
}

output "web_client_id" {
  description = "App client ID de la SPA WEB (sin secret, PKCE). Lo consume Amplify/la SPA de flota."
  value       = aws_cognito_user_pool_client.web.id
}

output "test_client_id" {
  description = "App client ID de TEST (ADMIN_NO_SRP_AUTH, sin callback web). Para validar la API con curl + JWT (AdminInitiateAuth) sin depender de Amplify."
  value       = aws_cognito_user_pool_client.test.id
}

output "identity_pool_id" {
  description = "ID del Identity Pool de flota (us-east-1:GUID). Federa los JWT a credenciales AWS de corta vida."
  value       = aws_cognito_identity_pool.fleet.id
}

output "authenticated_role_arn" {
  description = "ARN del rol IAM authenticated read-only que el Identity Pool entrega a los operadores autenticados."
  value       = aws_iam_role.authenticated.arn
}

output "authenticated_role_name" {
  description = "Nombre del rol IAM authenticated read-only."
  value       = aws_iam_role.authenticated.name
}

output "operators_group_name" {
  description = "Nombre del grupo de operadores (claim cognito:groups)."
  value       = aws_cognito_user_group.operators.name
}

output "admins_group_name" {
  description = "Nombre del grupo de administradores (claim cognito:groups)."
  value       = aws_cognito_user_group.admins.name
}
