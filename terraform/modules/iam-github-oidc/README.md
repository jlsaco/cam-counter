# Módulo `iam-github-oidc` — OIDC de GitHub Actions + roles plan/deploy separados

Provisiona el **proveedor OIDC** de GitHub Actions y **DOS roles IAM SEPARADOS** con
separación estricta de privilegios (plan vs apply), para la cuenta `950639281773` /
`us-east-1`, prefijo `cam-counter-`.

| Recurso | Nombre | Para qué |
| --- | --- | --- |
| `aws_iam_openid_connect_provider` | `token.actions.githubusercontent.com` | Confianza OIDC GitHub→AWS, acotada a `repo:jlsaco/cam-counter` |
| `aws_iam_role` (PLAN) | `cam-counter-gha-plan` | CI `terraform plan` **solo lectura** (pull_request + main). **No** puede aplicar. |
| `aws_iam_role` (DEPLOY) | `cam-counter-gha-deploy` | `apply` gated a `environment:prod`/`main`/tags. **Uso operativo futuro.** NUNCA `pull_request`. |

---

## Mapeo EXACTO del claim `sub` de GitHub → rol

GitHub Actions emite el claim `sub` de forma distinta según el contexto del job. El trust de
cada rol se acota por ese `sub` (y `aud = sts.amazonaws.com` SIEMPRE):

| Contexto del job en GitHub | Claim `sub` emitido | Rol que puede asumir |
| --- | --- | --- |
| Pull request | `repo:jlsaco/cam-counter:pull_request` | **PLAN** |
| Push a `main` | `repo:jlsaco/cam-counter:ref:refs/heads/main` | **PLAN** (plan-on-main) |
| Tag `vX.Y.Z` | `repo:jlsaco/cam-counter:ref:refs/tags/vX.Y.Z` | **DEPLOY** (uso futuro) |
| Job con `environment: prod` | `repo:jlsaco/cam-counter:environment:prod` | **DEPLOY** (uso futuro) |

Notas:

- `aud` (audience) es **siempre** `sts.amazonaws.com`.
- El trust **nunca** usa wildcard de repo (`repo:*`): siempre `repo:jlsaco/cam-counter`.
- El rol **DEPLOY no es asumible desde `pull_request`** (un PR malicioso no puede invocar
  apply). El rol **PLAN sí** incluye `pull_request` para que los jobs de plan en PR puedan
  asumirlo.
- **CI usa SÓLO el rol PLAN** (plan-only). El `apply` de la infraestructura de la pila lo
  ejecuta el **runner MAD** de forma autónoma con las credenciales de **su entorno** (no
  este rol, no GitHub Actions) — ver F2.
- Los **workflows futuros de release/promote** (PR11) que asuman el rol **DEPLOY** para
  escribir **OBJETOS S3** (artefactos OTA / manifiestos de canal) **NO** ejecutan
  `terraform apply` de infraestructura: publicar objetos S3 con un rol gated por
  Environment **no es** apply de infra y **no viola** la regla "CI plan-only".

---

## Decisiones de diseño

### Thumbprint del proveedor OIDC

Se **omite** `thumbprint_list` (`= []`). Desde julio de 2023 AWS asegura el endpoint
`token.actions.githubusercontent.com` con su propia librería de CAs de confianza, y el
proveedor AWS (`>= v5`) gestiona el thumbprint automáticamente. Omitirlo es la opción **más
idempotente**: no hay valor derivado en cliente (p.ej. vía `data "tls_certificate"`) que
pueda hacer *drift* cuando GitHub rota su certificado de hoja, lo que rompería el requisito
duro de idempotencia (F1: un plan desde checkout limpio debe dar **0 cambios**).

### Escritura mínima del lock para el rol PLAN

El rol PLAN es de **solo lectura** de los recursos del producto, pero `terraform plan`
necesita **adquirir y soltar el lock** de DynamoDB. Por eso su política incluye
`dynamodb:GetItem`/`PutItem`/`DeleteItem`/`DescribeTable` **acotado por ARN exclusivamente a
la tabla de lock** (`cam-counter-tfstate-lock`). Es la **única** escritura permitida al rol
PLAN; no puede crear/modificar/borrar ningún recurso de producto.

### Huevo y gallina del proveedor OIDC (resuelto de forma PERSISTENTE)

- **PREFERIDO (este repo)**: `create_oidc_provider = true` (default). El proveedor se crea y
  queda en el **state compartido** de `environments/prod`. Applies posteriores de la pila
  (p.ej. PR04, que reaplica este root) lo ven ya en el state y **convergen a 0 cambios**. Si
  AWS ya tuviera el proveedor creado fuera de Terraform, la resolución persistente es
  `terraform import aws_iam_openid_connect_provider.github_actions[0] <arn>` (manteniendo el
  default `true`).
- **ALTERNATIVA**: fijar `create_oidc_provider = false` y `oidc_provider_arn` en el
  **HCL/tfvars commiteado** del root (NUNCA un `-var` efímero de CLI), para que la decisión
  sea estable entre applies de la pila.

> En la cuenta `950639281773` **no existía** el proveedor antes de PR03, así que se aplicó la
> vía PREFERIDA: creación con `create_oidc_provider = true` y persistencia en el state
> compartido.

---

## Tags (F3) y la restricción CASE-INSENSITIVE de AWS IAM

El esquema F3 de la pila aplica a la vez los tags **capitalizados**
(`{ Project, ManagedBy = "terraform", Env }`) y los **minúscula**
(`project = "cam-counter"`, `managed_by = "mad-runner"`). Esto funciona en S3/DynamoDB
(claves de tag **sensibles** a mayúsculas), pero **AWS IAM trata las claves de tag como
CASE-INSENSITIVE** y `CreateRole` **rechaza** claves que difieren sólo en mayúsculas
(`Project`/`project`, `ManagedBy`/`managed_by`: «Duplicate tag keys found»).

Resolución (sin tocar el proveedor por defecto de la raíz que dejó PR02): este módulo es de
**un único proveedor** (valida en standalone), y la raíz lo instancia pasándole un proveedor
AWS dedicado **`aws.iam`** como su `aws` por defecto, vía `providers = { aws = aws.iam }`.
`aws.iam` (declarado en `terraform/environments/prod/providers.tf`) lleva `default_tags`
**IAM-safe**, aplicados a TODOS los recursos del módulo:

| Recurso del módulo | `default_tags` efectivos | F3 |
| --- | --- | --- |
| `aws_iam_openid_connect_provider` | `{ Env, project=cam-counter, managed_by=mad-runner }` | clave **minúscula** `managed_by=mad-runner` presente |
| `aws_iam_role` (plan/deploy) | `{ Env, project=cam-counter, managed_by=mad-runner }` | ídem; `CreateRole` ya no colisiona |

Así se satisface la verificación F3 sobre los roles
(`aws iam list-role-tags … managed_by=mad-runner`, en MINÚSCULA) sin que `CreateRole` falle.
Las claves **capitalizadas** `Project`/`ManagedBy` se **omiten** en los recursos IAM (porque
`Project`/`project` y `ManagedBy`/`managed_by` colisionan al ser IAM case-insensitive); siguen
presentes en S3/DynamoDB (PR02), que distinguen mayúsculas. La clave capitalizada `ManagedBy`
**nunca** toma el valor `"mad-runner"`.

---

## Modelo de despliegue (dos actores)

- **F2 — CI plan-only por diseño**: GitHub Actions CI **sólo** asume el rol **PLAN** y
  ejecuta `terraform plan` read-only vía OIDC. **Ningún** workflow de GitHub Actions ejecuta
  `terraform apply` de infraestructura. El `apply` de la pila lo realiza el **runner MAD** de
  forma autónoma con las credenciales de **su entorno** (jamás commiteadas), tras inspeccionar
  el plan para confirmar que es estrictamente aditivo.
- **F1 — State aditivo y monótono**: hay un único state de producción compartido por toda la
  pila. Se aplica **sólo** desde la rama apilada más alta; **nunca** se reaplica una rama
  inferior tras una superior; se **aborta** ante cualquier `destroy`/`replace` en el plan.

---

## Teardown

Recursos IAM/OIDC de **costo cero**. Para desmontarlos:

```bash
terraform -chdir=terraform/environments/prod destroy
```

(Destruye toda la pila instanciada en el root; ver advertencias del README del entorno sobre
el orden frente al state-backend.) Los tags `project = cam-counter` / `managed_by = mad-runner`
(minúscula) facilitan la trazabilidad y la limpieza.

---

## Variables principales

| Variable | Default | Descripción |
| --- | --- | --- |
| `github_org` / `github_repo` | `jlsaco` / `cam-counter` | Acotan el trust OIDC a `repo:<org>/<repo>`. |
| `create_oidc_provider` | `true` | Crea el proveedor OIDC (persistente en el state). |
| `oidc_provider_arn` | `""` | ARN de un proveedor preexistente (sólo si `create_oidc_provider = false`). |
| `tfstate_bucket_name` | — (requerido) | Acota la política read-only del rol PLAN al state real. |
| `tfstate_lock_table_name` | — (requerido) | Acota la escritura mínima del lock del rol PLAN. |
| `plan_role_name` / `deploy_role_name` | `cam-counter-gha-plan` / `cam-counter-gha-deploy` | Nombres de los roles. |
| `tags` | `{}` | Tags lógicos minúscula (F3) mergeados en todos los recursos. |

## Outputs

`oidc_provider_arn`, `plan_role_arn`, `deploy_role_arn`.
