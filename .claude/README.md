# `.claude/` — Convenciones para agentes que trabajen en `cam-counter`

Este directorio guarda la configuración y las convenciones que deben seguir los **agentes
automatizados** (y las personas) al trabajar en el monorepo `cam-counter`. El documento
maestro de arquitectura es `../CLAUDE.md`; aquí se concentra el **procedimiento operativo**.

- `settings.json` — configuración mínima del agente (JSON válido, sin comentarios).
- `README.md` — este documento.

---

## 1. Modelo de PRs apilados (stacked) y nombres de rama

- La iniciativa se entrega como una **pila de PRs apilados**. Cada PR ramifica **sobre la
  rama del PR anterior** (PR00 ramifica sobre `main`) y se abre con `base` = rama del PR
  anterior.
- **Convención de nombres de rama**: `feat/NN-...` (p. ej. `feat/00-bootstrap`,
  `feat/01-contracts`), cada una apilada sobre la anterior.
- Cada PR es **pequeño-mediano**, con **verificación automática fuerte ejecutable en x86 CI**
  (sin Pi / Hailo / cámara: el `DummyDetector` permite probar toda la lógica de conteo). Los
  **PRs de infra** añaden además verificación contra **recursos AWS REALES** tras el apply
  autónomo del runner.

---

## 2. Política de merge SIN squash

- Se permite **merge-commit** o **rebase-merge**. **PROHIBIDO `squash`**.
- **Por qué**: `squash` reescribe la historia de la base y **desincroniza** las ramas
  apiladas superiores.

---

## 3. Procedimiento de rebase de la pila TRAS CADA MERGE

Tras mergear `PRn` a `main`, hay que **rebasar todas las ramas superiores** para que sigan
apiladas sobre la nueva `main`. Para **cada** rama superior `PRn+1, PRn+2, …` (de abajo
hacia arriba):

```bash
# <base-antigua> = la rama sobre la que se abrió originalmente <rama-superior>
git fetch origin
git rebase --onto main <base-antigua> <rama-superior>
# resolver conflictos si los hubiera, luego:
git push --force-with-lease
```

Repetir en orden ascendente (PRn+1, luego PRn+2, …). Usar **siempre**
`--force-with-lease` (nunca `--force` a secas) para no pisar trabajo remoto.

---

## 4. Convención de despliegue de infra (runner vs. CI)

- En los **PRs de infra**, el **RUNNER MAD** ejecuta `terraform apply -auto-approve` de
  forma **AUTÓNOMA** usando las credenciales de **su ENTORNO** (jamás commiteadas), contra
  la cuenta **REAL** `950639281773` / `us-east-1`, con **state remoto compartido**
  (`terraform/environments/prod`) y **lock en DynamoDB**. Cada apply es **idempotente** y
  **verifica recursos reales** en su Definition of Done
  (`aws s3api head-bucket`, `aws dynamodb describe-table`, `aws iam get-role` /
  `get-open-id-connect-provider`).
- Este apply **no es humano**: lo realiza el runner MAD de forma autónoma, **antes** del
  merge humano. **Tampoco** está *merge-gated* (esta convención reemplaza cualquier idea
  previa de que ese paso fuese responsabilidad de una persona).
- **GitHub Actions CI permanece SOLO-PLAN**: `terraform fmt` / `validate` / `plan` vía OIDC,
  read-only. CI **NUNCA** ejecuta `terraform apply` de infraestructura.
- **MATIZACIÓN**: los **workflows de release / promote SÍ publican objetos S3** (artefactos
  y manifiestos) con el **rol de deploy `cam-counter-gha-deploy`** *gated* por GitHub
  Environment — eso **NO es apply de infraestructura**, es publicación de artefactos, y es
  legítimo.
- **Recursos etiquetados (F3)**: `Project` / `ManagedBy` / `Env` **capitalizados** con
  `ManagedBy = "terraform"`, **más** `project` / `managed_by` en **minúscula** con
  `managed_by = "mad-runner"`. **Nunca** se usa la clave capitalizada con el valor del
  runner. **Teardown** vía `terraform destroy` documentado.

---

## 5. Invariantes transversales (F1 / F2 / F3) — resumen

Todo PR de infra **DEBE** respetar:

- **F1 — State aditivo y monótono**: un solo state `terraform/environments/prod` compartido
  por la pila. Sólo se aplica desde la **rama apilada más alta**; **nunca** se reaplica una
  inferior tras una superior. **Inspeccionar `terraform plan`** y **ABORTAR** ante
  **cualquier `destroy` / `replace`** de un recurso existente. El plan debe ser
  **estrictamente aditivo** (solo "to add").
- **F2 — Apply autónomo acotado y justificado**: sólo **módulos curados** de la pila,
  **nunca** HCL arbitrario de terceros. CI permanece **plan-only** para **cerrar la
  escalada** de privilegios desde PRs no confiables. Trade-off declarado y aceptado.
- **F3 — Tags unificados**: `ManagedBy = "terraform"` (clave **capitalizada**) **+**
  `managed_by = "mad-runner"` (clave en **minúscula**). **Nunca** la clave capitalizada
  `ManagedBy` con el valor del runner.

---

## 6. Regla de CERO secretos

- **Nunca** commitear credenciales (claves AWS de larga vida, contraseñas, credenciales de
  cámara, etc.).
- GitHub Actions usa **OIDC** (**sólo para plan** en CI, read-only).
- El **apply autónomo** del runner usa las **credenciales de su entorno** (jamás
  commiteadas). Las credenciales de cámara van por **SSM / env / SQLite**, nunca en git.
- `gitleaks` correrá en CI más adelante.
