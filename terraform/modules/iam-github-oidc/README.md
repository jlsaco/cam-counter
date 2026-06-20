# terraform/modules/iam-github-oidc

Proveedor **OIDC de GitHub Actions** (`token.actions.githubusercontent.com`) + **DOS
roles IAM SEPARADOS** (privilegio mínimo, separación *plan* vs *apply*) para la cuenta
`950639281773` / `us-east-1`, repo curado `jlsaco/cam-counter`.

| Recurso | Para qué |
| --- | --- |
| `aws_iam_openid_connect_provider` | IdP federado de GitHub Actions; `client_id_list = ["sts.amazonaws.com"]`. |
| `cam-counter-gha-plan` (rol) | **SOLO LECTURA**. Lo asume **CI** vía OIDC para `terraform plan`. |
| `cam-counter-gha-deploy` (rol) | **apply** (uso operativo futuro). Gated; **NUNCA** desde `pull_request`. |

---

## Dos actores (no confundir)

- **RUNNER MAD** — aplica la **infraestructura** de forma **autónoma** con las
  credenciales de **su entorno** (jamás commiteadas). **NO** usa estos roles.
- **GitHub Actions CI** — **SOLO-PLAN**. Asume el rol `plan` vía OIDC y ejecuta
  `terraform plan` de solo lectura. **NUNCA** ejecuta `terraform apply` de infra (F2:
  esto cierra la escalada de privilegios desde PRs no confiables).

El rol `deploy` queda **creado** para operación futura: p.ej. los workflows de
release/promote (PR11) que publican **OBJETOS S3** (artefactos OTA + manifiestos de
canal). Publicar objetos S3 con un rol de deploy gated **NO es** `terraform apply` de
infraestructura, por lo que **NO** viola el principio "CI plan-only" (que se refiere a
infraestructura).

---

## Mapeo EXACTO `sub` (claim de GitHub) → rol

GitHub emite el claim `sub` en el token OIDC según el contexto del job. `aud` es
**siempre** `sts.amazonaws.com`. El trust **nunca** usa wildcard de repo (`repo:*`);
siempre se acota a `repo:jlsaco/cam-counter`.

| Contexto del job en GitHub | Claim `sub` emitido | Rol que puede asumirse |
| --- | --- | --- |
| Pull request | `repo:jlsaco/cam-counter:pull_request` | **PLAN** |
| Push a `main` | `repo:jlsaco/cam-counter:ref:refs/heads/main` | **PLAN** (plan-on-main) |
| Tag `vX.Y.Z` | `repo:jlsaco/cam-counter:ref:refs/tags/vX.Y.Z` | **DEPLOY** (operación futura) |
| Job con `environment: prod` | `repo:jlsaco/cam-counter:environment:prod` | **DEPLOY** (operación futura) |

- El trust del rol **PLAN** incluye el `sub` de `pull_request` (para que los jobs de
  plan en PR puedan asumirlo) y el de `main`.
- El trust del rol **DEPLOY** está restringido a `environment:prod`, `refs/heads/main`
  y `refs/tags/*`, y **NO** incluye `pull_request` — un PR malicioso **no** puede
  invocar `apply`.
- **CI usa SÓLO el rol PLAN** (plan-only). El `apply` de la pila lo realiza el RUNNER
  MAD con las credenciales de su entorno (F2).

---

## Política de cada rol (privilegio mínimo)

### `cam-counter-gha-plan` — SOLO LECTURA
- Lectura para `terraform plan`: `iam:Get*`/`iam:List*`, `s3:Get*`/`s3:List*`,
  `dynamodb:Describe*`/`dynamodb:List*`/`dynamodb:GetItem`, `sts:GetCallerIdentity`.
- Acceso al **estado remoto**: la lectura del `.tfstate` entra por `s3:Get*`/`s3:List*`.
- **Única escritura permitida**: `dynamodb:PutItem`/`DeleteItem` **acotada por ARN** a la
  tabla de lock (`cam-counter-tfstate-lock`), imprescindible para **adquirir/soltar el
  lock** del plan. No hay ninguna otra escritura.
- **No** incluye creación/modificación/borrado de recursos de producto → **no puede
  hacer apply**.

### `cam-counter-gha-deploy` — apply (uso futuro)
- Estado remoto completo (S3 del `.tfstate` + lock DynamoDB).
- Recursos del producto acotados al prefijo `cam-counter-*`: `s3:*` sobre buckets
  `cam-counter-*`, `dynamodb:*` sobre tablas `cam-counter-*`, `iam:*` sobre
  roles/políticas `cam-counter-*`.
- Lectura global para refresh/plan durante el apply.

---

## Idempotencia y persistencia del proveedor OIDC (huevo-y-gallina)

- En esta cuenta **no** existía el proveedor OIDC; el módulo lo **crea**
  (`create_oidc_provider = true`, default).
- Si en otra cuenta el proveedor **ya existiera** fuera del state, la resolución es
  **PERSISTENTE** (no un `-var` efímero de CLI), de dos formas equivalentes:
  1. **PREFERIDO**: `terraform import` del proveedor existente al recurso
     `aws_iam_openid_connect_provider.this[0]`, dejando `create_oidc_provider = true`.
  2. **ALTERNATIVA**: fijar `create_oidc_provider = false` y `oidc_provider_arn = "<arn>"`
     en el **HCL/tfvars commiteado** del root (bloque `module`), nunca en CLI.
- Así un `plan` desde un **checkout limpio** del root (p.ej. el apply de PR04) sigue
  dando **0 cambios**.

### Thumbprint: estático y documentado
El `thumbprint_list` se fija de forma **ESTÁTICA** a los thumbprints conocidos del IdP de
GitHub. Razón: **idempotencia/persistencia (F1)** sin dependencia de red ni *drift* por
rotación de certificados (un `data "tls_certificate"` recalcularía el fingerprint en cada
`plan`, arriesgando un cambio en el apply de PR04). Desde 2023 **AWS no valida** el
thumbprint para este IdP *well-known* (lo verifica contra CAs de confianza): el campo es
sólo un requisito de forma del recurso.

---

## Tags (F3)

Todos los recursos llevan los tags lógicos en **MINÚSCULA** `project = "cam-counter"` y
`managed_by = "mad-runner"` (vía `var.tags`) **MÁS** los `default_tags` capitalizados
heredados de la raíz prod `{ Project = "cam-counter", ManagedBy = "terraform",
Env = "prod" }`. La verificación de `managed_by=mad-runner` busca la clave en
**minúscula**. La clave capitalizada `ManagedBy` vale **siempre** `terraform`; **nunca**
se usa `ManagedBy = "mad-runner"`.

---

## State aditivo y monótono (F1)

Hay **UN** solo state de producción (`environments/prod`), con lock en DynamoDB,
compartido por toda la pila. El runner aplica **sólo desde la rama apilada más alta** con
todo el HCL acumulado; **nunca** reaplica una rama inferior tras una superior. Antes de
cada `apply`: `plan` + inspección; ante **cualquier** `destroy`/`replace`, **abortar**.

## Teardown

Recursos IAM/OIDC de **costo cero**. Para desmontar (sólo al desmontar la pila completa):

```bash
terraform -chdir=terraform/environments/prod destroy
```

Los tags `project=cam-counter` / `managed_by=mad-runner` (minúscula) facilitan la
trazabilidad y limpieza.

---

## Variables de entrada principales

| Variable | Default | Descripción |
| --- | --- | --- |
| `github_org` | `jlsaco` | Org/usuario del repo. |
| `github_repo` | `cam-counter` | Nombre del repo. |
| `create_oidc_provider` | `true` | Crea el IdP OIDC; `false` si ya existe (persistente). |
| `oidc_provider_arn` | `""` | ARN del IdP existente (sólo si `create_oidc_provider = false`). |
| `tfstate_bucket_name` | — | Bucket del state remoto (acota lectura del rol plan). |
| `tfstate_lock_table_name` | — | Tabla de lock (única escritura del rol plan). |
| `plan_role_name` | `cam-counter-gha-plan` | Nombre del rol plan. |
| `deploy_role_name` | `cam-counter-gha-deploy` | Nombre del rol deploy. |
| `tags` | `{}` | Tags lógicos minúscula (F3). |

## Outputs

`oidc_provider_arn`, `plan_role_arn`, `deploy_role_arn`.
