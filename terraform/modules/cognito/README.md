# Módulo `cognito` — User Pool de operadores de flota + clients + Identity Pool + grupos

Provisiona la **autenticación de operadores de la CONSOLA DE FLOTA cloud** (la SPA servida por
Amplify; **no** la UI local del Pi, que no usa Cognito). Self-signup **OFF**, **MFA TOTP**
obligatoria, app client web **SPA sin secret** (Authorization Code + **PKCE**), un app client de
**TEST** sin callback web (`ADMIN_NO_SRP_AUTH`) para validar la API con **curl + JWT**, un
**Identity Pool** que federa los JWT a credenciales AWS de corta vida con un rol
`authenticated` **read-only**, y los grupos **operators**/**admins**. Cuenta `950639281773` /
`us-east-1`.

**Independiente del camino IoT** (Things/certs/mTLS): aquí se autentican **personas**, no
devices. Apila sobre **WP09**; es **aditivo** (sólo añade recursos nuevos; no toca PR02–PR04,
PR11 ni el IoT Credentials Provider).

---

## Recursos creados (9)

| # | Recurso | Nombre canónico | Notas |
| --- | --- | --- | --- |
| 1 | `aws_cognito_user_pool` | `cam-counter-fleet-users` | Self-signup OFF, MFA TOTP, username=email, password fuerte. |
| 2 | `aws_cognito_user_pool_domain` | `cam-counter-fleet-950639281773` | Hosted UI (`<prefix>.auth.us-east-1.amazoncognito.com`). |
| 3 | `aws_cognito_user_pool_client` (web) | `cam-counter-fleet-web-client` | **Sin secret**, Auth Code + **PKCE**, flujos OAuth Hosted UI. |
| 4 | `aws_cognito_user_pool_client` (test) | `cam-counter-fleet-test-client` | **Sin secret**, **sin callback web**, `ALLOW_ADMIN_USER_PASSWORD_AUTH`. |
| 5 | `aws_cognito_identity_pool` | `cam-counter-fleet-identity` | `allow_unauthenticated_identities = false`. |
| 6 | `aws_iam_role` (authenticated) | `cam-counter-fleet-auth-role` | Trust federado a Cognito (aud=pool, amr=authenticated). |
| 7 | `aws_iam_role_policy` | `cam-counter-fleet-auth-role-policy` | Read-only DynamoDB + GetObject media, TLS-only. |
| 8 | `aws_cognito_identity_pool_roles_attachment` | — | Mapea (6) como rol `authenticated`. |
| 9 | `aws_cognito_user_group` ×2 | `cam-counter-admins` / `cam-counter-operators` | Precedencia 1 / 10; claim `cognito:groups`. |

---

## Decisiones de diseño

### Self-signup OFF + MFA TOTP
`admin_create_user_config.allow_admin_create_user_only = true` ⇒ los operadores **sólo** se
crean por administrador (ver `scripts/cognito-create-admin.sh`). `mfa_configuration = "ON"` +
`software_token_mfa_configuration` ⇒ **TOTP obligatoria**; **sin SMS** (no requiere rol de SNS).

### App client web: SPA sin secret + PKCE
`generate_secret = false` ⇒ cliente público ⇒ **Authorization Code + PKCE** (no implicit, no
secret en el navegador). `allowed_oauth_flows = ["code"]`, Hosted UI sólo con proveedor
`COGNITO`. `prevent_user_existence_errors = ENABLED` (no revela si un usuario existe).

### App client de TEST — resuelve la dependencia oculta WP11→WP13
El login web real (Auth Code + PKCE) necesita un **callback válido** que **sólo existe tras
Amplify (WP13)**. Para que el acceptance de WP11 («`curl` con JWT → 200») sea alcanzable **en su
propio PR**, este client de TEST usa `ALLOW_ADMIN_USER_PASSWORD_AUTH` (ADMIN_NO_SRP_AUTH), **sin
secret**, **sin callback web** ni flujos OAuth de cliente. Permite:

```bash
aws cognito-idp admin-initiate-auth \
  --user-pool-id "$(terraform -chdir=terraform/environments/prod output -raw cognito_user_pool_id)" \
  --client-id   "$(terraform -chdir=terraform/environments/prod output -raw cognito_test_client_id)" \
  --auth-flow ADMIN_USER_PASSWORD_AUTH \
  --auth-parameters USERNAME="$CAMCOUNTER_ADMIN_EMAIL",PASSWORD="$CAMCOUNTER_ADMIN_PASSWORD" \
  --query 'AuthenticationResult.IdToken' --output text
# → JWT para `curl -H "Authorization: Bearer <JWT>" https://<api>/...`
```

### Reconciliación WP13 (callback in-place)
`callback_urls` / `logout_urls` arrancan con un **PLACEHOLDER** del dominio Amplify por defecto
(`https://main.placeholder.amplifyapp.com/`). En el provider AWS **`~> 5.x`**, `callback_urls` y
`logout_urls` son atributos **actualizables** del recurso `aws_cognito_user_pool_client` (la API
`UpdateUserPoolClient` los modifica): **NO** son `ForceNew`. Por tanto WP13 reconcilia el
dominio real con un **update IN-PLACE** (`~ update`), no un `force-new`/replace del client (su
`id` no cambia, así que Amplify/la SPA no necesitan re-cablear el client_id).

### Identity Pool + rol authenticated read-only
El Identity Pool federa los JWT (web y test) a credenciales AWS de corta vida (SigV4). El rol
`authenticated` se asume con `sts:AssumeRoleWithWebIdentity` acotado por
`cognito-identity.amazonaws.com:aud = <identity_pool_id>` y `amr = authenticated`. Su política
es **READ-ONLY least-privilege**:

| Servicio | Acciones | Recurso / condición |
| --- | --- | --- |
| DynamoDB | `GetItem`, `BatchGetItem`, `Query`, `DescribeTable` | tablas events/devices **+ `/index/*`**, `Bool aws:SecureTransport = true` |
| S3 media | `s3:GetObject` | `…media-…/media/*`, `Bool aws:SecureTransport = true` |

**SIN** `Put*` / `Update*` / `Delete*` ni `ListBucket`. La diferencia operador/admin se resuelve
a nivel de app vía el claim `cognito:groups` (ambos grupos comparten este rol read-only).

### Dos proveedores (F3 + IAM case-insensitive)
El módulo recibe `aws` (por defecto, F3 dual-case) **y** `aws.iam` (IAM-safe). Los recursos de
Cognito usan el proveedor por defecto (sus tags toleran dual-case); el **rol IAM** usa `aws.iam`
para evitar «Duplicate tag keys» en `CreateRole` (AWS IAM trata las claves de tag como
**case-insensitive**). Mismo patrón que `iam-edge` / `iot-credential-provider`.

---

## Cero secretos
- Los app clients **no** generan secret (no hay secret que commitear).
- El **usuario admin** se crea **fuera de Terraform** con `scripts/cognito-create-admin.sh`
  (AdminCreateUser): la **password va por `env` `CAMCOUNTER_ADMIN_PASSWORD`**, **nunca** en git
  ni en el tfstate. Ver el script para el detalle.

---

## Verificación (DoD)

```bash
UP=$(terraform -chdir=terraform/environments/prod output -raw cognito_user_pool_id)
# Self-signup OFF + MFA TOTP:
aws cognito-idp describe-user-pool --user-pool-id "$UP" \
  --query 'UserPool.{mfa:MfaConfiguration,adminOnly:AdminCreateUserConfig.AllowAdminCreateUserOnly}'
# Client SPA sin secret + PKCE (code):
aws cognito-idp describe-user-pool-client --user-pool-id "$UP" \
  --client-id "$(terraform -chdir=terraform/environments/prod output -raw cognito_web_client_id)" \
  --query 'UserPoolClient.{secret:ClientSecret,flows:AllowedOAuthFlows}'
# Identity Pool sin no-auth:
aws cognito-identity describe-identity-pool \
  --identity-pool-id "$(terraform -chdir=terraform/environments/prod output -raw cognito_identity_pool_id)" \
  --query '{noAuth:AllowUnauthenticated}'
```
