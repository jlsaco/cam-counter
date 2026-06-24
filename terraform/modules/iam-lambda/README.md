# Módulo `iam-lambda` — rol de ejecución + política inline least-privilege POR función

Provisiona **un rol de ejecución IAM por función Lambda** con una **política inline
least-privilege** acotada a los ARNs exactos que esa función necesita. Cuenta
`950639281773` / `us-east-1`.

**Un rol por función, NUNCA compartido.** Cada Lambda del plano cloud
(`events-ingest`, `devices-register`, `line-publish`, `clip-presign`, `fleet-api`) se
instancia por separado con sus propios ARNs. El rol es además **distinto** del rol de
borde `cam-counter-edge-{site}-{device}` (módulo `iam-edge`): los Pis y las Lambdas no
comparten identidad.

En vez de duplicar HCL por función, este módulo se reutiliza: `module "lambda_events_ingest"
{ source = "../../modules/iam-lambda"; function_short_name = "events-ingest"; ... }`.

---

## Naming — gate de coherencia HCL ↔ doc

El nombre se deriva del slug `function_short_name` (`{dominio}-{accion}`, dominio primero):

| Recurso | Plantilla | Ejemplo (`function_short_name = "events-ingest"`) |
| --- | --- | --- |
| **Lambda** | `cam-counter-{function_short_name}` | `cam-counter-events-ingest` |
| **Rol de ejecución** | `cam-counter-{function_short_name}-role` | `cam-counter-events-ingest-role` |
| **Política inline** | `cam-counter-{function_short_name}-policy` | `cam-counter-events-ingest-policy` |

Estos patrones son **idénticos** a `docs/naming-standard.md` §5 (tabla de Lambdas + roles) y
§11 (gate de coherencia). El `default` del HCL iguala **carácter a carácter** el canon del
doc:

| Función (naming-standard §5) | Rol esperado (== canon) |
| --- | --- |
| `cam-counter-events-ingest` | `cam-counter-events-ingest-role` |
| `cam-counter-devices-register` | `cam-counter-devices-register-role` |
| `cam-counter-line-publish` | `cam-counter-line-publish-role` |

### Reconciliación de naming

El issue WP03 esbozaba `role_name = cam-counter-lambda-{short}-role` (con infijo `-lambda-`).
Se **reconcilia** al patrón **sin infijo** de `docs/naming-standard.md` §5/§11
(`cam-counter-{dominio}-{accion}-role`), que es la **fuente de verdad** del gate de coherencia
(criterio de aceptación «Defaults HCL == naming-standard.md» y nota [ALTA] del revisor). El
doc **manda** sobre el esbozo del issue (CLAUDE.md: «si una spec antigua contradice el doc,
manda el doc»). Así `cam-counter-events-ingest-role` casa con la tabla del estándar y NO
introduce un nombre `cam-counter-lambda-…` que el estándar no contempla.

---

## Permisos concedidos (least-privilege, OPT-IN)

Sólo **CloudWatch Logs** se concede por defecto. Todo lo demás es **opt-in**: una variable
vacía / lista vacía **omite por completo** su statement.

| # | Servicio | Cuándo | Acciones (default) | Recurso / condición |
| --- | --- | --- | --- | --- |
| 1 | **CloudWatch Logs** | siempre | `CreateLogGroup`, `CreateLogStream`, `PutLogEvents` | `…:log-group:/aws/lambda/cam-counter-{name}:*` (sólo el grupo propio) |
| 2 | **DynamoDB** | `dynamodb_table_arns` no vacío | `PutItem`, `UpdateItem` | tablas + `dynamodb_gsi_arns` EXACTOS (nunca `*`) |
| 3 | **S3** | `s3_bucket_arn` no vacío | `GetObject` | `${s3_bucket_arn}/${s3_prefix}` (default `media/*`), **`aws:SecureTransport = true`** |
| 4 | **SQS DLQ** | `sqs_dlq_arn` no vacío | `SendMessage` | sólo la cola `sqs_dlq_arn` |
| 5 | **X-Ray** | `enable_xray = true` | `PutTraceSegments`, `PutTelemetryRecords` | `*` (X-Ray no admite resource-level) |
| 6 | **Extra** | `extra_policy_statements` no vacío | (lo que se pase) | cada statement acota sus propios `resources` |

**Invariantes de least-privilege (criterios de aceptación):**

- **Sin `Scan` ni `DeleteItem`** en DynamoDB por defecto (`dynamodb_actions` = PutItem/UpdateItem).
- **Sin `PutObject` ni `DeleteObject`** en S3 por defecto (`s3_actions` = GetObject).
- **Nunca** referencia los buckets `cam-counter-fleet-releases-*`, `cam-counter-tfstate-*` ni
  `cam-counter-rpi-artifacts-*`: no son recursos de plano de datos de estas Lambdas.
- DynamoDB y S3 acotados a **ARNs/prefijos exactos**; S3 además **TLS-only**.
- Logs acotados al **grupo propio** `/aws/lambda/cam-counter-{name}` (no a `*`).

---

## Ejemplos de instanciación (en la raíz `environments/prod`)

```hcl
# Ingesta de eventos de cruce → escribe en cam-counter-events.
module "lambda_events_ingest" {
  source              = "../../modules/iam-lambda"
  providers           = { aws = aws.iam } # IAM-safe para tags F3
  function_short_name = "events-ingest"
  dynamodb_table_arns = [module.events_table.table_arn]
  # dynamodb_actions default = [PutItem, UpdateItem]
  tags = { project = "cam-counter", managed_by = "mad-runner" }
}

# Heartbeat/registro de device → upsert en cam-counter-devices (+ GSI por canal).
module "lambda_devices_register" {
  source              = "../../modules/iam-lambda"
  providers           = { aws = aws.iam }
  function_short_name = "devices-register"
  dynamodb_table_arns = [module.device_registry.table_arn]
  dynamodb_gsi_arns   = ["${module.device_registry.table_arn}/index/GSI1"]
  tags                = { project = "cam-counter", managed_by = "mad-runner" }
}

# Presign de clips → lectura del bucket de media (sólo GetObject, TLS-only).
module "lambda_clip_presign" {
  source              = "../../modules/iam-lambda"
  providers           = { aws = aws.iam }
  function_short_name = "clip-presign"
  s3_bucket_arn       = module.media_bucket.bucket_arn
  s3_prefix           = "media/*"
  # s3_actions default = [GetObject]
  tags = { project = "cam-counter", managed_by = "mad-runner" }
}
```

> **F3 / tags**: como `iam-edge` e `iam-github-oidc`, la raíz pasa el proveedor IAM-safe
> `aws.iam` (`providers = { aws = aws.iam }`). AWS IAM trata las claves de tag como
> CASE-INSENSITIVE; el esquema dual-case de `default_tags` (Project/project, ManagedBy/managed_by)
> haría fallar `CreateRole`. El proveedor `aws.iam` aplica el subconjunto IAM-safe { Env,
> project=cam-counter, managed_by=mad-runner }, y `local.tags` garantiza la clave minúscula
> `managed_by=mad-runner`.

---

## Variables principales

| Variable | Default | Descripción |
| --- | --- | --- |
| `function_short_name` | — (requerido) | Slug `{dominio}-{accion}`; deriva role/policy name. |
| `name_prefix` | `cam-counter` | Prefijo de producto del nombre del rol/política. |
| `dynamodb_table_arns` | `[]` | ARNs de tablas (vacío = sin acceso DynamoDB). |
| `dynamodb_actions` | `["dynamodb:PutItem","dynamodb:UpdateItem"]` | Sin Scan/DeleteItem. |
| `dynamodb_gsi_arns` | `[]` | ARNs de índices (GSI/LSI) consultados. |
| `s3_bucket_arn` | `""` | ARN del bucket (vacío = sin acceso S3). |
| `s3_prefix` | `media/*` | Prefijo acotado dentro del bucket. |
| `s3_actions` | `["s3:GetObject"]` | Sin PutObject/Delete. |
| `sqs_dlq_arn` | `""` | ARN de la DLQ (vacío = sin SQS). |
| `enable_xray` | `false` | Tracing activo de X-Ray. |
| `extra_policy_statements` | `[]` | Escotilla controlada (cada statement acota sus recursos). |
| `aws_account_id` / `aws_region` | `950639281773` / `us-east-1` | Para el ARN del log group. |
| `tags` | `{}` | Tags lógicos minúscula (F3). |

## Outputs

| Output | Descripción |
| --- | --- |
| `role_arn` | ARN del rol de ejecución (se asigna a `aws_lambda_function.role`). |
| `role_name` | Nombre del rol (`cam-counter-{function_short_name}-role`). |

---

## Verificación

```bash
terraform -chdir=terraform/modules/iam-lambda fmt -check -diff
terraform -chdir=terraform/modules/iam-lambda init -backend=false
terraform -chdir=terraform/modules/iam-lambda validate
```

Cuando se instancie en `environments/prod`, el **plan debe ser estrictamente aditivo**
(F1): sólo «to add» de los nuevos roles/políticas, sin `destroy`/`replace` de recursos de
PR02–PR04/PR11. Una vez aplicado, comprobar el least-privilege con `simulate-principal-policy`
sobre el rol real (p. ej. que `dynamodb:Scan` o `s3:PutObject` salgan `implicitDeny`).

---

## Nota de hardening futuro (NO bloqueante)

El rol OIDC `cam-counter-gha-deploy` (módulo `iam-github-oidc`) **no** incluye hoy permisos
de `lambda:*` ni de creación de roles de ejecución de Lambda. Esto **NO bloquea** este WP:
MAD aplica el Terraform como la **identidad admin `raspberry`** (`AdministratorAccess`,
`~/.aws`), **no** vía el rol `cam-counter-gha-deploy`. Cuando una futura iteración mueva el
apply de las Lambdas a CI/CD vía `gha-deploy`, habrá que ampliar su política con un statement
acotado a `arn:aws:iam::950639281773:role/cam-counter-*-role` (PassRole + gestión del rol de
ejecución) y a `arn:aws:lambda:...:function:cam-counter-*`. Queda registrado aquí como
**hardening futuro**, sin crear un WP bloqueante.

---

## Teardown

```bash
terraform -chdir=terraform/environments/prod destroy -target=module.lambda_events_ingest
```

Costo: recursos IAM de **costo cero**.
