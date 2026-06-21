# `terraform/environments/prod/` — entorno de producción (raíz live)

Composición raíz del **único** entorno de producción de la pila de infra. Aquí se
instancian los módulos de `terraform/modules/` y vive el **único state de producción**
compartido por toda la pila apilada (backend **S3** + **lock en DynamoDB**).

En **PR02** esta raíz instancia únicamente el módulo `state-backend` (bucket de `.tfstate`
+ tabla de lock). PR03/PR04/…/PR11 irán añadiendo más módulos sobre **este mismo state**.

---

## Invariantes (ver `CLAUDE.md`, §5 y §6)

- **F1 — State aditivo y monótono.** Hay **un** solo state de producción compartido por
  toda la pila. El runner MAD **sólo** aplica desde la rama apilada **más alta** que
  contiene todo el HCL acumulado; **nunca** se reaplica una rama inferior tras una
  superior (reaplicar PR02 tras PR03/PR04 propondría **destruir** los recursos que PR02 no
  conoce). **Antes de cada `apply`**: `terraform plan` + **inspección**; si aparece
  **cualquier `destroy`/`replace`** de un recurso existente, **ABORTAR**. El plan debe ser
  **estrictamente aditivo** (sólo "to add").
- **F2 — Apply autónomo acotado y justificado.** El **runner MAD** ejecuta
  `terraform apply` de forma **autónoma**, **antes** del merge humano, con las credenciales
  de **su entorno** (jamás commiteadas). Se justifica **exclusivamente** porque el HCL
  proviene de esta **pila curada** y porque el plan se inspecciona para ser **estrictamente
  aditivo** (F1). El runner **no** aplica HCL arbitrario de terceros; en PR02 el apply se
  **restringe** al módulo `state-backend`. **GitHub Actions CI permanece plan-only** (no usa
  credenciales AWS, no hace `plan`/`apply`), lo que cierra la escalada de privilegios desde
  PRs no confiables.
- **F3 — Tags unificados.** `default_tags` lleva los CAPITALIZADOS
  `{ Project = "cam-counter", ManagedBy = "terraform", Env = "prod" }` **más** los lógicos
  en **minúscula** `project = "cam-counter"` y `managed_by = "mad-runner"`, aplicados a
  **todos** los recursos. La clave capitalizada `ManagedBy` **siempre** vale `"terraform"`;
  **nunca** `"mad-runner"`.

---

## Bootstrap AUTÓNOMO del backend (lo ejecuta el RUNNER MAD, no un humano ni el CI)

El backend remoto sufre un problema de **huevo y gallina**: no podemos guardar el `.tfstate`
en un bucket S3 que aún no existe. Se resuelve con un **bootstrap en dos fases**,
**idempotente**, que **ejecuta el runner MAD** con las credenciales de su entorno:

### Fase 1 — estado local → crear el backend

Sin `backend.tf` activo presente (sólo existe `backend.tf.example`):

```bash
terraform -chdir=terraform/environments/prod init          # estado LOCAL
terraform -chdir=terraform/environments/prod plan          # INSPECCIONAR (F1)
# F1: el plan debe ser ESTRICTAMENTE ADITIVO (sólo crear el bucket de tfstate y la
# tabla de lock, o "sin cambios" si ya existen). Si aparece CUALQUIER destroy/replace
# de un recurso existente -> ABORTAR, no aplicar, reportar el bloqueo.
terraform -chdir=terraform/environments/prod apply -auto-approve   # CREA en AWS REAL
```

**Idempotencia.** Si el bucket o la tabla ya existen de una ejecución previa pero no están
en el state, **impórtalos** en lugar de fallar, de modo que el apply converja a "sin
cambios":

```bash
terraform -chdir=terraform/environments/prod import \
  module.state_backend.aws_s3_bucket.tfstate    cam-counter-tfstate-950639281773
terraform -chdir=terraform/environments/prod import \
  module.state_backend.aws_dynamodb_table.lock  cam-counter-tfstate-lock
```

### Fase 2 — migrar el state al backend remoto

```bash
cp terraform/environments/prod/backend.tf.example terraform/environments/prod/backend.tf
terraform -chdir=terraform/environments/prod init -migrate-state -force-copy
```

Esto mueve el `.tfstate` local al bucket S3 (`key = environments/prod/terraform.tfstate`)
con lock en DynamoDB. Tras migrar, **`terraform plan` debe dar SIN CAMBIOS**.

> En este repo **se commitea `backend.tf` activo** (recomendado): así PR03+ usan el backend
> remoto sin pasos manuales. El backend **no** contiene secretos.

### Verificación (idempotencia + recursos reales)

```bash
terraform -chdir=terraform/environments/prod plan -detailed-exitcode   # exit 0 = sin cambios
aws s3api head-bucket --bucket cam-counter-tfstate-950639281773
aws dynamodb describe-table --table-name cam-counter-tfstate-lock \
  --query 'Table.{Keys:KeySchema,Billing:BillingModeSummary.BillingMode}'
aws dynamodb describe-continuous-backups --table-name cam-counter-tfstate-lock \
  --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus'  # ENABLED
aws s3api head-object --bucket cam-counter-tfstate-950639281773 \
  --key environments/prod/terraform.tfstate
```

Atajo: `make -C terraform bootstrap` ejecuta las dos fases; `make -C terraform verify-backend`
hace las comprobaciones contra AWS.

---

## Monotonía (F1): no reaplicar esta rama tras una superior

Una vez que PR03/PR04/…/PR11 hayan aplicado contra **este mismo state**, **no** se debe
volver a aplicar desde la rama de PR02 (ni ninguna rama inferior): su HCL no conoce los
módulos superiores y el plan propondría **destruirlos**. Aplica siempre desde la **rama
apilada más alta**.

---

## Teardown

Recursos de **bajo costo** (S3 + DynamoDB on-demand). Para desmontar **todo** el backend:

```bash
make -C terraform teardown      # = terraform -chdir=environments/prod destroy
```

⚠️ **Advertencia.** Destruir el state-backend mientras el state vive **dentro** de él es
delicado: primero hay que **sacar el state** del bucket (volver a backend local con
`terraform init -migrate-state` apuntando a local, o `terraform state pull` a un fichero) o
el destroy se quedará sin dónde escribir. Hazlo **sólo** al desmontar **toda** la pila de
infra, y **después** de haber destruido los recursos de PR03/PR04/…/PR11.
