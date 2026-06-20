# `terraform/` — Infraestructura como código de `cam-counter`

Terraform (HCL) para la cuenta AWS `950639281773`, región `us-east-1`, prefijo de recursos
`cam-counter-`. Layout **directorio-por-entorno** (no workspaces):

```
terraform/
├── Makefile                      # atajos fmt/validate/bootstrap/plan/apply/destroy
├── README.md                     # este fichero
├── modules/
│   └── state-backend/            # PR02: bucket de .tfstate + tabla de lock DynamoDB
└── environments/
    └── prod/                     # raíz live: ÚNICO state de producción compartido
        ├── backend.tf            # backend S3 ACTIVO (commiteado tras el bootstrap)
        ├── backend.tf.example    # plantilla del backend (referencia)
        └── README.md             # procedimiento de bootstrap AUTÓNOMO (F1/F2/F3)
```

## Modelo de despliegue (dos actores, no confundir)

- **RUNNER MAD** — aplica de forma **AUTÓNOMA** (`terraform apply -auto-approve`) contra
  AWS REAL con las credenciales de **su entorno** (jamás commiteadas), **antes** del merge
  humano. SIEMPRE inspecciona el `plan` para garantizar que es **estrictamente aditivo**
  (F1) y aborta ante cualquier `destroy`/`replace`. Se limita a los módulos enumerados de
  cada PR (F2).
- **GitHub Actions CI** — **SOLO-PLAN**: `fmt -check`, `validate` (con `-backend=false`) y
  `tflint`. **SIN** credenciales AWS, **SIN** `plan`, **SIN** `apply`. En PRs posteriores
  hará a lo sumo `plan` read-only vía OIDC.

## Invariantes de la pila (ver `CLAUDE.md` §5/§6)

- **F1 — State aditivo y monótono.** Un solo state `environments/prod`; aplicar sólo desde
  la rama apilada más alta; nunca reaplicar una inferior tras una superior; abortar ante
  cualquier `destroy`/`replace` en el plan.
- **F2 — Apply autónomo acotado.** El runner aplica HCL curado pre-merge; CI plan-only.
- **F3 — Tags unificados.** `default_tags` capitalizados `{Project, ManagedBy=terraform,
  Env}` + lógicos minúscula `project`/`managed_by=mad-runner` en TODOS los recursos.

## Targets del Makefile

| Target            | Actor      | Qué hace                                                            |
| ----------------- | ---------- | ------------------------------------------------------------------ |
| `fmt`             | dev        | `terraform fmt -recursive .`                                       |
| `fmt-check`       | **CI**     | `terraform fmt -check -recursive .`                               |
| `validate`        | **CI**     | `init -backend=false` + `validate` de módulo y raíz               |
| `tflint`          | **CI**     | `tflint` sobre módulo y raíz                                       |
| `bootstrap`       | **RUNNER** | Bootstrap en dos fases del backend (idempotente, plan-aditivo F1) |
| `plan`            | RUNNER     | `plan` contra backend remoto (inspeccionar antes de aplicar)      |
| `apply`           | **RUNNER** | `apply -auto-approve` (tras inspección F1)                        |
| `verify-backend`  | RUNNER     | Comprueba que bucket + tabla EXISTEN en AWS                        |
| `destroy`/`teardown` | RUNNER  | Desmonta el backend (ver advertencia en `environments/prod/`)     |

## Bootstrap del backend de estado (PR02)

Procedimiento completo y razonado en
[`environments/prod/README.md`](environments/prod/README.md). Resumen:

```bash
make -C terraform bootstrap        # fase 1 (local→crear) + fase 2 (migrate-state)
make -C terraform verify-backend   # head-bucket / describe-table / head-object
```

El bootstrap es **idempotente**: un segundo `make -C terraform plan` con backend remoto da
"0 to add, 0 to change, 0 to destroy". Si los recursos ya existen pero no están en el state,
se **importan** (`terraform import`) en lugar de fallar (ver README del entorno).

## Validación estática (lo que también corre el CI, sin credenciales)

```bash
make -C terraform fmt-check
make -C terraform validate
```
