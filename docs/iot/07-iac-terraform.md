# IaC: modulos Terraform nuevos

Diseno de los modulos Terraform nuevos para cam-counter (cuenta `950639281773`, region `us-east-1`), siguiendo EXACTAMENTE el patron existente: `terraform/modules/<servicio>` consumidos desde `terraform/environments/prod`, estado remoto S3 (`cam-counter-tfstate-950639281773`) + lock DynamoDB (`cam-counter-tfstate-lock`), MAD aplica `terraform apply -auto-approve` de forma autonoma, GHA CI plan-only, y estado **aditivo/monotono** (se aborta ante cualquier destroy/replace de recurso existente).

> Convencion de las SPECs: hay divergencias menores de naming entre las SPECs de fundaciones (p.ej. thing name `cam-counter-edge-{site}-{device}` vs `cam-counter-{site}-{device}`, policy `cam-counter-edge-device-policy` vs `cam-counter-device-policy` vs `cam-counter-edge-policy`, topic `cam-counter/{thing}/events` vs `cam-counter/{device_id}/events/crossing` vs `cam-counter/evt/...`). Estos modulos **parametrizan** esos nombres y patrones de topic via variables (con defaults), de modo que la decision final de naming se fija UNA vez en `environments/prod` sin tocar los modulos. Cada modulo abajo indica su variable de naming.

---

## Resumen de modulos nuevos

| # | Modulo | Responsabilidad | Scope |
|---|---|---|---|
| 1 | `iam-lambda` | Roles de ejecucion + politicas inline least-privilege para cada Lambda. Fundacion IAM reutilizable. | por-flota |
| 2 | `iot-core` | Thing Type, Thing Groups (flota/sitio/canal), IoT Policy plantilla, IoT Rules + rol de topic-rule, named shadows base. | por-flota |
| 3 | `iot-credential-provider` | Rol `cam-counter-edge-s3-role` + IoT role alias para subida de clips a S3 con credenciales temporales (mTLS), sin llaves estaticas. | por-flota |
| 4 | `iot-provisioning` | Provisioning template + claim policy + pre-provisioning hook (cableado a la Lambda `device-register`). Things/certs por-device se materializan fuera de TF (script/provisioning). | por-flota |
| 5 | `lambda-ingest` | Lambda(s) de ingesta (events-ingest, device-status) + DLQ SQS + CloudWatch Log Group + permisos IoT-Rule->Lambda. | por-flota |
| 6 | `api-dashboard` | API HTTP (API Gateway v2) + Lambda backend (`dashboard-api` / `clip-presign`) + Cognito JWT authorizer + stage `prod`. | por-flota |
| 7 | `cognito` | User Pool de operadores + domain + app clients + Identity Pool + roles auth/unauth. | por-flota |
| 8 | `amplify-app` | App Amplify Hosting (Next.js consola de flota) + branch `main`->env prod + variables de entorno (apunta a API/Cognito). | por-flota |

Recursos **por-device** (Thing concreto, certificado, item DynamoDB del registro, named shadows materializados de un thing) NO los crea Terraform: los crea el script `provision-device.sh` / Fleet Provisioning (estado `provisioning`, tag `ManagedBy=provisioning`). Terraform solo define la **infra de flota compartida** (lo invariante: types, groups, policies, templates, roles, rules, lambdas). Esto preserva el invariante aditivo/monotono: agregar un device nunca toca el estado Terraform.

---

## 1. Modulo `iam-lambda`

Responsabilidad: fabricar, de forma uniforme, el rol de ejecucion + la politica inline least-privilege de cada Lambda del proyecto. Un rol por funcion (nunca compartido). Se invoca una vez por Lambda. Distinto del rol de borde `cam-counter-edge-{site}-{device}`.

Variables clave:
- `function_short_name` (string) — p.ej. `events-ingest`, `device-status`, `clip-presign`, `dashboard-api`, `device-register`. Deriva `role_name = cam-counter-lambda-{short}-role`.
- `dynamodb_table_arns` (list, default `[]`) + `dynamodb_actions` (default `["dynamodb:PutItem","dynamodb:UpdateItem"]`).
- `dynamodb_gsi_arns` (list, default `[]`) — para roles de lectura (dashboard) que consultan GSI1.
- `s3_bucket_arn` (string, default `""`) + `s3_prefix` (default `media/*`) + `s3_actions` (default `["s3:GetObject"]`).
- `sqs_dlq_arn` (string, default `""`) — concede `sqs:SendMessage` a la DLQ.
- `enable_xray` (bool, default false), `extra_policy_statements` (list, default `[]`) — escotilla para casos puntuales (p.ej. `iot:UpdateThingShadow` del dashboard).
- `tags`.

Outputs clave: `role_arn`, `role_name`.

Notas least-privilege: sin `Scan`/`DeleteItem` en roles de ingesta; sin `PutObject`/`Delete` en roles de lectura; siempre `Condition aws:SecureTransport=true` en S3; log group ARN scoping `/aws/lambda/cam-counter-{...}:*`. Buckets `fleet-releases`, `tfstate`, `rpi-artifacts` NUNCA aparecen.

---

## 2. Modulo `iot-core`

Responsabilidad: toda la topologia IoT compartida de flota.
- Thing Type (1, inmutable; searchable attrs `site_id`,`device_id`,`release_channel`).
- Thing Groups: raiz `cam-counter-fleet` (estatico), por-sitio `cam-counter-site-{site_id}` (estaticos, hijos de la raiz), por-canal `cam-counter-channel-{stable|beta|canary}` (dinamicos, query `attributes.release_channel:<canal>`).
- IoT Policy plantilla (1, attach por-cert) con policy variables `${iot:Connection.Thing.ThingName}` / `${iot:Connection.Thing.Attributes[...]}` para aislamiento por-device.
- IoT Rules (events/status/lwt) + el **topic rule role** (`cam-counter-iot-rule-role`) que las Rules asumen para invocar Lambda / escribir CloudWatch + error action.
- Habilitacion de Fleet Indexing (registry + shadow) para queries por sitio/canal.

Variables clave:
- `iot_thing_type_name` (default `cam-counter-rpi`).
- `iot_policy_name` (default `cam-counter-device-policy`).
- `site_ids` (list) — para materializar los grupos por-sitio (estaticos).
- `release_channels` (default `["stable","beta","canary"]`) — grupos dinamicos por-canal.
- `topic_namespace` (default `cam-counter`) y `event_topic_filter` (default `cam-counter/+/events/crossing`) — parametriza el patron de topic de la SPEC elegida.
- `events_lambda_arn`, `status_lambda_arn` (inputs desde `lambda-ingest`) — targets de las Rules.
- `account_id`, `region`, `tags`.

Outputs clave: `iot_policy_arn`, `iot_policy_name`, `thing_type_name`, `fleet_group_arn`, `site_group_arns` (map por site_id), `channel_group_arns` (map por canal), `iot_rule_role_arn`, `event_topic_filter`. Estos outputs los consume `iot-provisioning` (policy/groups/type para el template) y el script de provisioning.

Dependencias: requiere los ARNs de las Lambdas (`lambda-ingest`) como input -> `lambda-ingest` se aplica antes (o se cablea via output, ver orden).

---

## 3. Modulo `iot-credential-provider`

Responsabilidad: permitir que el device suba clips a S3 con credenciales **temporales** via IoT Credentials Provider, reusando su cert mTLS — eliminando la necesidad de la llave estatica del user `raspberry` para S3.
- Rol `cam-counter-edge-s3-role` con trust en `credentials.iot.amazonaws.com`.
- Politica: solo `s3:PutObject` sobre `cam-counter-media-950639281773/media/*`, acotada por prefijo del thing con `${credentials-iot:ThingName}` + `aws:SecureTransport=true`. Sin `GetObject`/`List`/`Delete`.
- IoT Role Alias `cam-counter-edge-s3-role-alias` (`aws_iot_role_alias`) apuntando al rol, `credential_duration_seconds` corto (default 3600).

Variables clave: `role_alias_name` (default `cam-counter-edge-role-alias`), `edge_s3_role_name` (default `cam-counter-edge-s3-role`), `media_bucket_name`/`media_bucket_arn`, `credential_duration_seconds` (default 3600), `tags`.

Outputs clave: `role_alias_name`, `role_alias_arn`, `edge_s3_role_arn`. El `role_alias_name` lo referencia la IoT Policy (`iot:AssumeRoleWithCertificate` sobre el rolealias) y el `.env` del contenedor (`CC_ROLE_ALIAS`).

> Nota de cableado: la IoT Policy (modulo 2) necesita el ARN del role alias para el `Sid:AssumeRoleForS3UploadCredentials`. Se cablea pasando `role_alias_arn` de este modulo como input de `iot-core`. Por eso `iot-credential-provider` se aplica **antes** de `iot-core` (o ambos en el mismo apply con dependencia via output).

---

## 4. Modulo `iot-provisioning`

Responsabilidad: infra de flota para vincular devices nuevos de forma repetible (Fleet Provisioning by claim, como evolucion; el bootstrap actual usa el script `provision-device.sh` que reusa policy/type/groups de este stack).
- Provisioning template `cam-counter-fleet-provisioning` (`aws_iot_provisioning_template`) que crea Thing + atributos + membership a grupos + attach de la policy, parametrizado por `{site_id,device_id,camera_id,release_channel}`.
- Claim policy `cam-counter-provisioning-claim-policy` (solo `$aws/provisioning-templates/*` y `$aws/certificates/*`).
- Cableado del pre-provisioning hook a la Lambda `cam-counter-device-register` (valida slug + no-duplicado, escribe item en `cam-counter-devices`).

Variables clave: `provisioning_template_name`, `claim_policy_name`, `device_policy_name` (output de `iot-core`), `thing_type_name`, `fleet_group_name`, `pre_provisioning_hook_lambda_arn` (output de `lambda-ingest`/`device-register`), `provisioning_role_arn`, `tags`.

Outputs clave: `provisioning_template_name`, `provisioning_template_arn`, `claim_policy_name`.

Dependencias: consume outputs de `iot-core` (policy/type/group names) y de la Lambda `device-register`. Para flota pequena actual es opcional/diferible; el script per-device cubre el bootstrap (estado `provisioning`, no Terraform). Modulo presente para no romper monotonia cuando se active Fleet Provisioning.

---

## 5. Modulo `lambda-ingest`

Responsabilidad: las Lambdas del data-plane de ingesta MQTT->DynamoDB y su infra de fiabilidad.
- Lambda `cam-counter-events-ingest` (alias `:live`): valida `crossing_event.schema.json`, anti-spoof (`topic(2)==payload.device_id`, `clientid()==thing`), conditional put idempotente en `cam-counter-events`, copia `clip_key`.
- Lambda `cam-counter-device-status`: upsert de connection/heartbeat/LWT en `cam-counter-devices` (incl. `GSI1PK=CHANNEL#{release_channel}`).
- (Opcional) `cam-counter-device-register` para el hook de provisioning.
- DLQ SQS `cam-counter-ingest-dlq` (eventos malformados / fallos de invocacion).
- CloudWatch Log Group por funcion (`/aws/lambda/cam-counter-events-ingest`, retencion configurable).
- `aws_lambda_permission` para que IoT Rules (principal `iot.amazonaws.com`, source ARN de la rule) invoquen las funciones.

Variables clave:
- `events_table_arn`, `devices_table_arn`, `devices_gsi_arns`.
- `media_bucket_arn` (HEAD del clip si aplica).
- `lambda_runtime` (default `python3.12`), `lambda_architectures` (default `["arm64"]`), `log_retention_days` (default 30), `memory_size`, `timeout`.
- `iot_rule_arns` (map; para `aws_lambda_permission` source_arn) — cableado cruzado con `iot-core`.
- `dlq_name` (default `cam-counter-ingest-dlq`).
- `iam_role_arns` (map por funcion, outputs de `iam-lambda`) — el modulo NO crea roles (los toma de `iam-lambda`).
- `package_*` (s3 bucket/key o filename del artefacto), `tags`.

Outputs clave: `events_lambda_arn`, `events_lambda_name`, `status_lambda_arn`, `device_register_lambda_arn`, `dlq_arn`, `dlq_url`, `log_group_names`. `events_lambda_arn`/`status_lambda_arn` se cablean a `iot-core` (Rule targets); `device_register_lambda_arn` a `iot-provisioning`.

> Ciclo events<->iot-core: `lambda-ingest` necesita `iot_rule_arns` (source de permission) y `iot-core` necesita `*_lambda_arn` (target de rule). Se rompe creando la Lambda primero (sin permission), luego la Rule en `iot-core`, y el `aws_lambda_permission` referenciando el ARN de la rule ya conocido por nombre deterministico. Orden: `iam-lambda` -> `lambda-ingest` -> `iot-core`.

---

## 6. Modulo `api-dashboard`

Responsabilidad: el control-plane de lectura para la consola de flota.
- API Gateway v2 (HTTP) `cam-counter-fleet-api`, stage `prod`.
- Lambda(s) backend: `cam-counter-dashboard-api` (Query read-only sobre `cam-counter-events`/GSI1 y `cam-counter-devices`/GSI1) y/o `cam-counter-clip-presign` (genera presigned GET de `clip_key` en `cam-counter-media-950639281773`, TTL corto).
- JWT authorizer `cam-counter-fleet-cognito-authorizer` apuntando al User Pool (modulo `cognito`).
- Routes (`GET /devices`, `GET /devices/{id}/events`, `GET /events/{id}/clip-url`) + integraciones Lambda + `aws_lambda_permission` para API Gateway.
- Log group de acceso del stage.

Variables clave: `api_name` (default `cam-counter-fleet-api`), `stage_name` (default `prod`), `cognito_user_pool_arn` + `cognito_client_id` (outputs de `cognito`), `dashboard_api_lambda_arn` / `clip_presign_lambda_arn` (creadas aqui o tomadas de `iam-lambda`+package), `events_table_arn`/`devices_table_arn`/`gsi_arns`, `media_bucket_arn`, `cors_allow_origins` (el dominio Amplify), `tags`.

Outputs clave: `api_endpoint` (invoke URL del stage prod), `api_id`, `authorizer_id`, `dashboard_api_lambda_arn`. `api_endpoint` lo consume `amplify-app` como variable de entorno del frontend.

Dependencias: requiere `cognito` (authorizer) y los roles/lambdas de lectura (`iam-lambda` para `dashboard-api-role`/`clip-presign-role`). Se aplica despues de `cognito`.

---

## 7. Modulo `cognito`

Responsabilidad: identidad de operadores de la consola de flota.
- User Pool `cam-counter-fleet-users` (alias `cam-counter-operators` segun SPEC; parametrizado): self-signup OFF, `AdminCreateUserConfig`, MFA TOTP, password policy fuerte.
- User Pool Domain `cam-counter-fleet-950639281773` (Hosted UI, sufijo de cuenta).
- App clients: web SPA `cam-counter-fleet-web-client` (sin secret, Auth Code + PKCE) y opcional server/SSR `cam-counter-fleet-server-client` (confidencial).
- Grupos `cam-counter-operators` (read-only flota), `cam-counter-admins`.
- Identity Pool `cam-counter-fleet-identity` + roles `cam-counter-fleet-cognito-auth-role` (least-privilege lectura) y unauth (denegado/minimo).

Variables clave: `user_pool_name`, `domain_prefix` (default `cam-counter-fleet-950639281773`), `callback_urls`/`logout_urls` (URLs Amplify, default placeholder), `mfa_configuration` (default `ON`), `enable_identity_pool` (default true), `tags`.

Outputs clave: `user_pool_id`, `user_pool_arn`, `web_client_id`, `hosted_ui_domain`, `identity_pool_id`, `auth_role_arn`. `user_pool_arn`+`web_client_id` -> `api-dashboard` (authorizer); `user_pool_id`+`web_client_id`+`hosted_ui_domain` -> `amplify-app` (env del frontend).

Dependencias: independiente; se puede aplicar temprano. `callback_urls` se completa con el dominio Amplify (ciclo suave: usar dominio default de Amplify `https://main.<appid>.amplifyapp.com` y actualizar tras crear la app, o pasar dominio custom conocido).

---

## 8. Modulo `amplify-app`

Responsabilidad: hosting de la app Next.js de consola de flota.
- `aws_amplify_app` `cam-counter-fleet-console` (repo `jlsaco/cam-counter`, build settings Next.js SSR/static, platform `WEB_COMPUTE` si SSR).
- `aws_amplify_branch` `main` -> env prod (auto-build); ramas feature -> previews.
- Variables de entorno de build/runtime: `NEXT_PUBLIC_API_ENDPOINT` (output de `api-dashboard`), `NEXT_PUBLIC_COGNITO_USER_POOL_ID`/`_CLIENT_ID`/`_DOMAIN` (outputs de `cognito`), `NEXT_PUBLIC_AWS_REGION`.
- (Opcional) `aws_amplify_domain_association` para `fleet.cam-counter.<dominio>`.

Variables clave: `app_name` (default `cam-counter-fleet-console`), `repository`, `oauth_token`/`access_token` (via secret, NO en git), `branch_name` (default `main`), `environment_variables` (map; recibe los outputs de api/cognito), `custom_domain` (opcional), `tags`.

Outputs clave: `amplify_app_id`, `default_domain` (`https://main.<appid>.amplifyapp.com`), `app_arn`. `default_domain` retroalimenta `cognito.callback_urls`/`logout_urls` y `api-dashboard.cors_allow_origins`.

Dependencias: ultimo en aplicarse (consume API + Cognito). Cierra el ciclo de callback con Cognito (resolver con dominio Amplify default o custom conocido de antemano).

---

## Orden de aplicacion (dependencias)

Grafo de dependencias entre modulos (flecha = "consume output de"):

```
state-backend (existente, ya aplicado)
        │
        ▼
iam-lambda ──────────────┐
   │                     │
   ▼                     ▼
iam-github-oidc(ext)  iot-credential-provider
                         │
lambda-ingest ◄──────────┤ (roles de iam-lambda)
   │                     │
   ▼                     ▼
iot-core ◄───────────────┘ (role_alias_arn + lambda arns + rule role)
   │
   ▼
iot-provisioning  (policy/type/groups + device-register lambda)

cognito  ───► api-dashboard ───► amplify-app
                  ▲                   │
                  └─── (callback/cors)┘  (ciclo suave: dominio Amplify default)
```

Orden topologico recomendado (cada paso = 1 PR apilado sobre el anterior, merge no-squash, MAD aplica auto-approve):

1. **`iam-lambda`** — fundacion de roles (sin deps; usa solo ARNs de tablas/buckets existentes).
2. **`iot-credential-provider`** — role alias para S3 (sin deps de las lambdas).
3. **`lambda-ingest`** — Lambdas + DLQ + log groups (usa roles de paso 1; aun sin permission de IoT Rule, o con nombre deterministico de rule).
4. **`iot-core`** — type/groups/policy/rules + topic-rule role (usa `*_lambda_arn` del paso 3 y `role_alias_arn` del paso 2); aqui se cierran los `aws_lambda_permission`.
5. **`iot-provisioning`** — template/claim/hook (usa outputs de pasos 3 y 4). Diferible para la flota pequena actual.
6. **`cognito`** — user pool/clients/identity (sin deps).
7. **`api-dashboard`** — API + authorizer + lambdas de lectura (usa paso 6 + roles del paso 1).
8. **`amplify-app`** — consola Next.js (usa pasos 6 y 7). Cierra callback/CORS con Cognito.

Ramas 1-5 (camino IoT/ingesta) y 6-8 (camino consola) son **independientes** y pueden apilarse en paralelo; convergen solo via el bucket de media y las tablas existentes. La migracion de fases (dual-write -> apagar put directo -> matar user `raspberry`) ocurre por configuracion del device (`SYNC_TRANSPORT`) y deny-policy fuera de estos modulos; el `delete-user raspberry` es el unico destroy y se ejecuta revisado por humano, NO por el runner MAD autonomo.

---

## Cableado en `environments/prod`

En `terraform/environments/prod/main.tf` (mismo patron de los modulos existentes: cada modulo un bloque `module`, outputs encadenados, `default_tags` del provider con `Project/Environment/ManagedBy/Repo/CostCenter`). Esquema:

```hcl
# --- Fundacion IAM de Lambdas (un module call por funcion, o un module con for_each) ---
module "iam_lambda_events_ingest" {
  source              = "../../modules/iam-lambda"
  function_short_name = "events-ingest"
  dynamodb_table_arns = [data.aws_dynamodb_table.events.arn, data.aws_dynamodb_table.devices.arn]
  sqs_dlq_arn         = module.lambda_ingest.dlq_arn   # o pasar nombre deterministico
  tags                = local.tags
}
module "iam_lambda_dashboard_api" {
  source              = "../../modules/iam-lambda"
  function_short_name = "dashboard-api"
  dynamodb_table_arns = [data.aws_dynamodb_table.events.arn, data.aws_dynamodb_table.devices.arn]
  dynamodb_gsi_arns   = [local.events_gsi1_arn, local.devices_gsi1_arn]
  dynamodb_actions    = ["dynamodb:Query","dynamodb:GetItem","dynamodb:BatchGetItem"]
  s3_bucket_arn       = data.aws_s3_bucket.media.arn   # presigned GET
  tags                = local.tags
}
# ... clip-presign, device-status, device-register analogos

module "iot_credential_provider" {
  source            = "../../modules/iot-credential-provider"
  media_bucket_name = "cam-counter-media-950639281773"
  media_bucket_arn  = data.aws_s3_bucket.media.arn
  tags              = local.tags
}

module "lambda_ingest" {
  source           = "../../modules/lambda-ingest"
  events_table_arn = data.aws_dynamodb_table.events.arn
  devices_table_arn= data.aws_dynamodb_table.devices.arn
  iam_role_arns = {
    events-ingest   = module.iam_lambda_events_ingest.role_arn
    device-status   = module.iam_lambda_device_status.role_arn
    device-register = module.iam_lambda_device_register.role_arn
  }
  iot_rule_arns = local.iot_rule_arns   # nombres deterministicos cam_counter_*
  tags          = local.tags
}

module "iot_core" {
  source            = "../../modules/iot-core"
  site_ids          = ["sitio-demo"]            # grupos por-sitio estaticos
  release_channels  = ["stable","beta","canary"]
  events_lambda_arn = module.lambda_ingest.events_lambda_arn
  status_lambda_arn = module.lambda_ingest.status_lambda_arn
  role_alias_arn    = module.iot_credential_provider.role_alias_arn
  account_id        = local.account_id
  region            = local.region
  tags              = local.tags
}

module "iot_provisioning" {
  source                          = "../../modules/iot-provisioning"
  device_policy_name              = module.iot_core.iot_policy_name
  thing_type_name                 = module.iot_core.thing_type_name
  fleet_group_name                = "cam-counter-fleet"
  pre_provisioning_hook_lambda_arn= module.lambda_ingest.device_register_lambda_arn
  tags                            = local.tags
}

module "cognito" {
  source        = "../../modules/cognito"
  callback_urls = [local.amplify_default_domain]   # o dominio custom
  logout_urls   = [local.amplify_default_domain]
  tags          = local.tags
}

module "api_dashboard" {
  source                = "../../modules/api-dashboard"
  cognito_user_pool_arn = module.cognito.user_pool_arn
  cognito_client_id     = module.cognito.web_client_id
  events_table_arn      = data.aws_dynamodb_table.events.arn
  devices_table_arn     = data.aws_dynamodb_table.devices.arn
  media_bucket_arn      = data.aws_s3_bucket.media.arn
  dashboard_api_role_arn= module.iam_lambda_dashboard_api.role_arn
  clip_presign_role_arn = module.iam_lambda_clip_presign.role_arn
  cors_allow_origins    = [module.amplify_app.default_domain]
  tags                  = local.tags
}

module "amplify_app" {
  source     = "../../modules/amplify-app"
  repository = "https://github.com/jlsaco/cam-counter"
  environment_variables = {
    NEXT_PUBLIC_API_ENDPOINT          = module.api_dashboard.api_endpoint
    NEXT_PUBLIC_COGNITO_USER_POOL_ID  = module.cognito.user_pool_id
    NEXT_PUBLIC_COGNITO_CLIENT_ID     = module.cognito.web_client_id
    NEXT_PUBLIC_COGNITO_DOMAIN        = module.cognito.hosted_ui_domain
    NEXT_PUBLIC_AWS_REGION            = local.region
  }
  tags = local.tags
}
```

Recursos existentes (`cam-counter-events`, `cam-counter-devices`, `cam-counter-media-950639281773`) se referencian con `data` sources (no se gestionan/duplican), preservando el invariante aditivo. El bucket `cam-counter-rpi-artifacts-950639281773` NO se referencia en ningun modulo.

Notas de cableado de ciclos:
- **events<->iot-core**: romper con nombres deterministicos de IoT Rule (`cam_counter_crossing_events`, etc.) en `locals`, de modo que `lambda-ingest` arme el `source_arn` del `aws_lambda_permission` sin depender del recurso rule (que vive en `iot-core`).
- **cognito<->amplify (callback/CORS)**: usar el dominio Amplify default conocido tras el primer apply (`https://main.<appid>.amplifyapp.com`) o un dominio custom fijado de antemano; un segundo apply idempotente reconcilia callbacks sin destroy/replace.

---

## Por-flota (compartido) vs por-device

| Recurso | Scope | Quien lo crea |
|---|---|---|
| Thing Type `cam-counter-rpi` | por-flota | `iot-core` (TF) |
| Thing Groups (fleet / site-* / channel-*) | por-flota | `iot-core` (TF) |
| IoT Policy plantilla (1, attach por-cert) | por-flota | `iot-core` (TF) |
| IoT Rules + topic-rule role | por-flota | `iot-core` (TF) |
| Role alias S3 + `cam-counter-edge-s3-role` | por-flota | `iot-credential-provider` (TF) |
| Provisioning template + claim policy | por-flota | `iot-provisioning` (TF) |
| Lambdas (ingest/status/register/presign/api) + DLQ + log groups | por-flota | `lambda-ingest`/`api-dashboard` (TF) |
| Roles de Lambda | por-flota | `iam-lambda` (TF) |
| Cognito (pool/clients/identity/roles) | por-flota | `cognito` (TF) |
| API Gateway + authorizer + stage | por-flota | `api-dashboard` (TF) |
| Amplify app + branch main | por-flota | `amplify-app` (TF) |
| **Thing concreto** `cam-counter-{site}-{device}` | **por-device** | script/Fleet Provisioning (`ManagedBy=provisioning`) |
| **Certificado X.509 + attach policy/thing** | **por-device** | script/Fleet Provisioning |
| **Item registro en `cam-counter-devices`** | **por-device** | Lambda `device-register` / script |
| **Named shadows materializados** (`line-config`/`config`,`command`/`ops`,`health`) de un thing | **por-device** | runtime device + dashboard (no TF) |
| **Membership del thing a groups** (site/channel) | **por-device** | provisioning template / script |
| Rol de borde `cam-counter-edge-{site}-{device}` (existente) | por-device | `iam-edge` (existente, se retira post-migracion) |

Regla invariante: Terraform gestiona SOLO lo por-flota (compartido, invariante en el numero de devices). Todo lo por-device queda fuera del estado Terraform para que agregar/quitar un device NUNCA produzca un diff/destroy en el plan de MAD — garantizando el estado aditivo/monotono y que MAD pueda aplicar `auto-approve` sin riesgo de destroy/replace.