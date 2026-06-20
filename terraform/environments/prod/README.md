# `terraform/environments/prod/` — entorno de producción

**Esqueleto; se implementa en PRs posteriores.**

Composición raíz del **único** entorno de producción de la pila de infra. Aquí se
instancian los módulos de `terraform/modules/` y vive el **único state de producción**
compartido por toda la pila apilada (backend S3 + lock en DynamoDB).

Invariantes (ver `CLAUDE.md`, §6):

- **F1 — State aditivo y monótono**: el runner MAD sólo aplica desde la rama apilada más
  alta; nunca se reaplica una rama inferior tras una superior; se **aborta** ante cualquier
  `destroy`/`replace` en el plan.
- **F2 — Apply autónomo acotado**: lo aplica el runner MAD antes del merge humano,
  restringido a los módulos curados de cada PR; CI sigue plan-only.
- **F3 — Tags unificados**: esquema idéntico de `default_tags` + tags lógicos en minúscula.

> Este PR (PR01) **no** ejecuta `terraform plan`/`apply` ni crea recursos.
