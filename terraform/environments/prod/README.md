# terraform/environments/prod — Entorno de producción (state remoto)

Composición raíz de Terraform para el entorno `prod` (único entorno del producto).
Instancia los módulos de `terraform/modules` y mantiene el **único state de
producción**, **aditivo y monótono** (F1), compartido por toda la pila de PRs de
infra (PR02 state backend → PR03 OIDC/roles → PR04 media/eventos/devices/IAM → … →
PR11 releases).

En **PR02** sólo se instancia el módulo `state-backend`:

- **Bucket S3 `cam-counter-tfstate-950639281773`** — versioning, SSE-S3 (AES256),
  Block Public Access (4 flags `true`), Object Ownership `BucketOwnerEnforced`,
  lifecycle que expira versiones no-actuales, y bucket policy **TLS-only-deny**.
- **Tabla DynamoDB `cam-counter-tfstate-lock`** — `hash_key = "LockID"`,
  `PAY_PER_REQUEST`, Point-in-Time Recovery habilitado.

---

## Bootstrap AUTÓNOMO en dos fases (lo ejecuta el RUNNER MAD)

> **Quién:** el **RUNNER MAD**, de forma **AUTÓNOMA**, con las credenciales de **su
> ENTORNO** (jamás commiteadas). **NO** lo ejecuta un operador humano **NI** el CI.
> El `apply` va **SIEMPRE** precedido de la inspección del `plan` (F1: estrictamente
> aditivo; abortar ante cualquier `destroy`/`replace`). El apply autónomo se
> justifica (F2) porque el HCL proviene de esta pila curada y el plan es aditivo.

El backend remoto no puede existir antes de crearse (huevo-y-la-gallina). Se
resuelve en dos fases. Todo está automatizado e **idempotente** en
[`../../bootstrap.sh`](../../bootstrap.sh) (`make -C terraform bootstrap`):

### Fase 1 — estado LOCAL → crear el backend
Sin `backend.tf` activo, Terraform usa estado **local** y `apply` **crea** el
bucket de tfstate y la tabla de lock:

```bash
cd terraform/environments/prod
rm -f backend.tf                         # asegura estado local en esta fase
terraform init -input=false
terraform plan -input=false -no-color | tee /tmp/pr02-bootstrap.plan
# F1: ABORTAR si aparece destroy/replace; sólo debe CREAR los 2 recursos (o "sin cambios").
grep -Eq 'will be destroyed|must be replaced' /tmp/pr02-bootstrap.plan && { echo "ABORTAR (F1)"; exit 1; } || true
terraform apply -input=false -auto-approve
```

### Fase 2 — migrar el state local a S3
Se activa el backend copiando la plantilla y se migra el `.tfstate` a S3 con lock:

```bash
cp backend.tf.example backend.tf
terraform init -input=false -migrate-state -force-copy
```

Tras la migración, en este repo `backend.tf` queda **commiteado y activo**, de modo
que **PR03+** usan el backend remoto sin pasos manuales.

### Idempotencia y verificación contra AWS REAL
```bash
terraform plan -input=false -detailed-exitcode    # exit 0 = "0 to add/change/destroy"

aws s3api head-bucket --bucket cam-counter-tfstate-950639281773
aws dynamodb describe-table --table-name cam-counter-tfstate-lock \
  --query 'Table.{Keys:KeySchema,Billing:BillingModeSummary.BillingMode}' --output json
aws dynamodb describe-continuous-backups --table-name cam-counter-tfstate-lock \
  --query 'ContinuousBackupsDescription.PointInTimeRecoveryDescription.PointInTimeRecoveryStatus' --output text  # ENABLED
aws s3api head-object --bucket cam-counter-tfstate-950639281773 --key environments/prod/terraform.tfstate
```

Un segundo `bootstrap.sh` detecta que el backend ya existe, inicializa contra el
backend remoto y confirma `plan -detailed-exitcode = 0` (sin cambios).

---

## F1 — state aditivo y monótono (regla de monotonía)

Hay **UN** solo state de producción (`environments/prod/terraform.tfstate` en el
bucket de tfstate) con lock en DynamoDB, **compartido** por toda la pila apilada.

- El runner aplica **sólo desde la rama apilada MÁS ALTA** con todo el HCL
  acumulado hasta ese punto.
- **NUNCA reapliques esta rama (PR02, la más baja) una vez que PR03/PR04/…/PR11
  hayan aplicado contra el mismo state.** La rama de PR02 no conoce los módulos
  superiores y su `plan` propondría **DESTRUIR** los recursos de los PRs superiores.
- Antes de cualquier `apply`: `plan` + inspección. Si aparece **cualquier**
  `destroy`/`replace` de un recurso existente, **ABORTA**.

## F2 — apply autónomo acotado y justificado

El runner aplica HCL **antes** del merge humano **exclusivamente** porque el HCL
proviene de esta pila controlada y curada y porque el plan se inspecciona para ser
estrictamente aditivo (F1). El runner **no** aplica HCL arbitrario de terceros: en
PR02 el apply se restringe al módulo `state-backend`. **GitHub Actions CI permanece
plan-only**, lo que cierra la escalada de privilegios desde PRs no confiables.

## F3 — tags unificados

`default_tags` capitalizados `{ Project = "cam-counter", ManagedBy = "terraform",
Env = "prod" }` **más** los lógicos en minúscula `project = "cam-counter"` y
`managed_by = "mad-runner"` en **todos** los recursos. La clave capitalizada
`ManagedBy` vale **siempre** `terraform`; **nunca** se usa `ManagedBy = "mad-runner"`.

---

## Teardown

Recursos de **bajo costo** (S3 + DynamoDB on-demand). Para desmontar el backend
(sólo al desmontar **toda** la pila de infra):

```bash
make -C terraform destroy        # = terraform -chdir=environments/prod destroy
```

> **Advertencia:** destruir el state-backend mientras el state vive **dentro** de
> él requiere primero **mover el state a backend local** (desactivar `backend.tf`
> y `terraform init -migrate-state` de vuelta a local), o vaciar/extraer el state.
> De lo contrario Terraform intentaría borrar el bucket que contiene su propio
> `.tfstate`. Hazlo **únicamente** al desmontar la iniciativa completa.

## Notas

- **No se commitea** `terraform.tfstate*` ni `.terraform/` (ver `.gitignore`).
- El `backend.tf` activo **no** contiene secretos (sólo nombres de bucket/tabla,
  región y `encrypt = true`); las credenciales viven en el entorno del runner.
