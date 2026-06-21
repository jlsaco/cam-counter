# `terraform/modules/` — módulos Terraform reutilizables

**Esqueleto; se implementa en PRs posteriores.**

Aquí vivirán los módulos Terraform por servicio (media bucket, tablas DynamoDB de
eventos/devices, IAM por-Pi, proveedor OIDC y roles de plan/deploy, bucket de releases),
consumidos por `terraform/environments/prod`.

Convenciones de la pila de infra (ver `CLAUDE.md`):

- Cuenta AWS `950639281773`, región `us-east-1`, prefijo `cam-counter-`.
- Estado remoto en **S3** + **lock en DynamoDB**; un único state de producción compartido.
- `default_tags` capitalizados `{ Project = "cam-counter", ManagedBy = "terraform",
  Env = "prod" }` **más** tags lógicos en minúscula `project = "cam-counter"` y
  `managed_by = "mad-runner"` en todos los recursos.
- El `terraform apply` lo ejecuta el **runner MAD** de forma autónoma; GitHub Actions CI
  permanece **plan-only**. Cada plan de infra debe ser **estrictamente aditivo**.

> Este PR (PR01) **no** crea recursos AWS ni HCL con providers: sólo deja el esqueleto.
