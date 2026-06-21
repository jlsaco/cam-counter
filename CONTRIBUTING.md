# Cómo contribuir a `cam-counter`

Gracias por contribuir. Este monorepo sigue convenciones estrictas para mantener la
historia limpia y la **pila de PRs apilados** sincronizada. El documento maestro de
arquitectura es [`CLAUDE.md`](./CLAUDE.md); las convenciones operativas para agentes están
en [`.claude/README.md`](./.claude/README.md).

---

## 1. Conventional commits

Usa **conventional commits** en el asunto:

- `feat:` — nueva funcionalidad.
- `fix:` — corrección de bug.
- `chore:` — mantenimiento (build, tooling, deps).
- `docs:` — documentación.
- `refactor:`, `test:`, `ci:`, `perf:`, `build:` — según corresponda.

Ejemplo: `chore: bootstrap monorepo docs, agent conventions y verify_toolchain`.

---

## 2. Nombres de rama y PRs apilados

- **Convención de rama**: `feat/NN-...` (p. ej. `feat/00-bootstrap`, `feat/01-contracts`).
- La iniciativa se entrega como una **pila de PRs apilados (stacked)**: cada PR ramifica
  **sobre la rama del PR anterior** (PR00 ramifica sobre `main`) y se abre con `base` =
  rama del PR anterior, para que el diff sea limpio y revisable **de abajo hacia arriba**.
- Mantén cada PR **pequeño-mediano** y con **verificación automática** ejecutable en x86 CI.

---

## 3. Protección de ramas

- `main` está **protegida**: no se hace push directo; todo entra por **Pull Request**
  revisado.
- Los checks de CI (lint, tests, `terraform fmt`/`validate`/`plan`, `gitleaks`) deben pasar
  antes del merge.
- Se requiere **revisión humana** (aprobación) antes de mergear a `main`.

---

## 4. Política de merge: **NUNCA `squash`**

- Se permite **merge-commit** o **rebase-merge**. **PROHIBIDO `squash`**.
- **Por qué**: `squash` reescribe la historia de la base y **desincroniza** las ramas
  apiladas superiores. La pila depende de preservar la base de cada rama.
- **Tras cada merge a `main`**, hay que **rebasar las ramas superiores** con
  `git rebase --onto main <base-antigua> <rama-superior>` y `git push --force-with-lease`
  (procedimiento detallado en [`.claude/README.md`](./.claude/README.md)).

---

## 5. Convención de despliegue de infraestructura (runner vs. CI)

- En los **PRs de infra**, el **RUNNER MAD** ejecuta `terraform apply` de forma
  **AUTÓNOMA** (`terraform apply -auto-approve`) usando las credenciales de **su ENTORNO**
  (jamás commiteadas), contra la cuenta **REAL** `950639281773` / `us-east-1`.
- Este apply **no es responsabilidad humana**: lo realiza el runner MAD de forma autónoma,
  **antes** del merge humano. **Tampoco** está *merge-gated*.
- **GitHub Actions CI permanece SOLO-PLAN**: `terraform fmt` / `validate` / `plan` vía
  OIDC, read-only. CI **NUNCA** ejecuta `terraform apply` de infraestructura (esto cierra
  la escalada de privilegios desde PRs no confiables).
- **MATIZACIÓN**: los **workflows de release / promote SÍ publican objetos S3** (artefactos
  y manifiestos) con el **rol de deploy `cam-counter-gha-deploy`** *gated* por GitHub
  Environment — eso **NO es apply de infraestructura**, es publicación de artefactos.
- Cada apply de infra respeta las invariantes transversales:
  - **F1 — state aditivo y monótono**: un solo state compartido; sólo se aplica desde la
    rama apilada más alta; **inspeccionar `terraform plan`** y **abortar** ante cualquier
    `destroy` / `replace`; el plan debe ser estrictamente aditivo.
  - **F2 — apply autónomo acotado y justificado**: sólo módulos curados de la pila, nunca
    HCL arbitrario; CI plan-only para cerrar la escalada.
  - **F3 — tags unificados**: `ManagedBy = "terraform"` (clave **capitalizada**) **+**
    `managed_by = "mad-runner"` (clave en **minúscula**); **nunca** la clave capitalizada
    con el valor del runner.

---

## 6. Regla de CERO secretos

- **Nunca** se commiten secretos: claves AWS de larga vida, contraseñas ni credenciales de
  cámara.
- GitHub Actions asume AWS vía **OIDC** (sólo para plan en CI). El apply autónomo del
  runner usa las **credenciales de su entorno** (jamás commiteadas). Las credenciales de
  cámara van por **SSM / env / SQLite**.
- `gitleaks` correrá en CI para detectar fugas.

---

## 7. Co-autoría de commits

Cuando aplique (p. ej. commits generados con asistencia de un agente), firma el commit con
el co-autor estándar al final del mensaje:

```
Co-Authored-By: Claude Opus 4.8 <noreply@anthropic.com>
```
