# terraform/ — Infraestructura como código de `cam-counter`

Terraform (HCL) para la cuenta AWS **`950639281773`**, región **`us-east-1`**,
prefijo de recursos **`cam-counter-`**. Un único entorno de producción con
**state remoto compartido** (S3 + lock en DynamoDB), aditivo y monótono.

```
terraform/
├── Makefile                     # atajos fmt/validate/tflint/bootstrap/plan/apply/destroy
├── bootstrap.sh                 # bootstrap AUTÓNOMO en dos fases (lo corre el RUNNER)
├── .tflint.hcl                  # config mínima de tflint (ruleset terraform + AWS)
├── modules/
│   └── state-backend/           # bucket de tfstate + tabla de lock DynamoDB  ← PR02
└── environments/
    └── prod/                    # composición raíz (único state de producción)
        ├── versions.tf
        ├── providers.tf         # region + allowed_account_ids + default_tags (F3)
        ├── main.tf              # module "state_backend"
        ├── outputs.tf
        ├── backend.tf           # backend S3 ACTIVO (commiteado tras el bootstrap)
        ├── backend.tf.example   # plantilla del backend (documentación / re-bootstrap)
        └── README.md            # procedimiento de bootstrap, F1/F2/F3 y teardown
```

## Quién ejecuta qué (modelo de despliegue)

| Acción                                   | Quién                | Dónde                          |
|------------------------------------------|----------------------|--------------------------------|
| `fmt-check`, `validate`, `tflint`        | **GitHub Actions CI**| `ubuntu-latest`, SIN credenciales, SIN `apply`/`plan` |
| `bootstrap`, `plan`, `apply`, `destroy`  | **RUNNER MAD**       | autónomo, credenciales de su ENTORNO (jamás commiteadas) |

- **CI = plan-only.** El job de Terraform usa `terraform init -backend=false` y
  **nunca** asume un rol AWS ni ejecuta `plan`/`apply`. Esto cierra la escalada de
  privilegios desde PRs no confiables.
- **El `apply` lo hace el RUNNER**, de forma autónoma y **antes** del merge humano,
  SIEMPRE tras inspeccionar que el `plan` es estrictamente **aditivo** (F1).

## Comandos

```bash
make -C terraform fmt-check     # CI: verifica formato
make -C terraform validate      # CI: init -backend=false + validate (módulo y raíz)
make -C terraform tflint        # CI: linter
make -C terraform bootstrap     # RUNNER: crea el backend (idempotente, dos fases)
make -C terraform plan          # RUNNER: plan (inspeccionar que sea aditivo, F1)
make -C terraform apply         # RUNNER: apply -auto-approve (tras inspeccionar el plan)
make -C terraform destroy       # RUNNER: teardown del backend (ver advertencia)
```

## Invariantes de la pila de infra (F1/F2/F3)

- **F1 — state aditivo y monótono.** Un solo state `environments/prod` compartido
  por toda la pila (PR02→PR03→PR04→…→PR11). Aplica **sólo** desde la rama apilada
  más alta con todo el HCL acumulado; **nunca** reapliques una rama inferior tras
  una superior. Antes de cada `apply`: `plan` + inspección; **aborta** ante
  cualquier `destroy`/`replace`.
- **F2 — apply autónomo acotado.** El runner aplica HCL pre-merge **sólo** porque
  proviene de esta pila curada y el plan se inspecciona aditivo; no aplica HCL de
  terceros. CI permanece plan-only.
- **F3 — tags unificados.** `default_tags` capitalizados
  `{ Project = "cam-counter", ManagedBy = "terraform", Env = "prod" }` **más** los
  lógicos en minúscula `project = "cam-counter"` y `managed_by = "mad-runner"` en
  **todos** los recursos. `ManagedBy` vale **siempre** `terraform`; **nunca**
  `mad-runner`.

El detalle del bootstrap en dos fases, idempotencia, verificación contra AWS real
y **teardown** está en [`environments/prod/README.md`](environments/prod/README.md).
