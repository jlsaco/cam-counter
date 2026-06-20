# terraform/environments/prod — Entorno de producción (esqueleto)

Esqueleto; **se implementa en PRs posteriores**.

Este directorio contendrá la **composición raíz** de Terraform para el entorno `prod`
(único entorno del producto): instancia los módulos de `terraform/modules` y mantiene el
**state aditivo y monótono** compartido por toda la pila de PRs de infra.

Reglas (ver `CLAUDE.md`, invariantes F1/F2/F3):

- **Un solo state de producción** en S3 con lock en DynamoDB.
- El runner MAD aplica **sólo** desde la rama apilada más alta; **nunca** reaplica una
  inferior tras una superior.
- **Antes de cada apply**: `terraform plan` inspeccionado; si aparece cualquier
  `destroy`/`replace` de un recurso existente, se **aborta**. El plan debe ser
  estrictamente **aditivo**.
- GitHub Actions CI permanece **plan-only** (nunca `apply` de infraestructura).

> Este PR (PR01) **no** ejecuta `terraform plan`/`apply` ni crea recursos AWS.
