# terraform/modules — Módulos Terraform (esqueleto)

Esqueleto; **se implementa en PRs posteriores**.

Aquí vivirán los **módulos Terraform por servicio** del producto `cam-counter`
(media bucket, tablas DynamoDB de eventos/devices, IAM por-Pi, roles OIDC de plan/deploy,
bucket de releases OTA, …), reutilizables desde `terraform/environments/prod`.

Convenciones (ver `CLAUDE.md`):

- Cuenta AWS `950639281773`, región `us-east-1`, prefijo de recursos `cam-counter-`.
- Estado remoto en **S3** con **lock en DynamoDB** (un único state de producción).
- `default_tags` capitalizados `{ Project = "cam-counter", ManagedBy = "terraform", Env = "prod" }`
  **más** tags lógicos en minúscula `project = "cam-counter"` y `managed_by = "mad-runner"`.

> Este PR (PR01) **no** crea recursos AWS ni HCL con providers: solo el esqueleto de carpetas.
