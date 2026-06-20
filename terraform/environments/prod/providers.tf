# Provider AWS de la raíz live de producción.
#
# F3 — TAGS UNIFICADOS: `default_tags` lleva a la vez los tags CAPITALIZADOS
# { Project, ManagedBy = "terraform", Env } y los tags lógicos en MINÚSCULA
# project = "cam-counter" y managed_by = "mad-runner". Ambos conjuntos se aplican a
# TODOS los recursos de la pila. La clave capitalizada `ManagedBy` SIEMPRE vale
# "terraform"; el valor "mad-runner" vive SÓLO en la clave minúscula `managed_by`.
provider "aws" {
  region = "us-east-1"

  # Cinturón de seguridad: aborta si las credenciales del entorno apuntan a otra cuenta.
  allowed_account_ids = ["950639281773"]

  default_tags {
    tags = {
      # Capitalizados (F3): ManagedBy SIEMPRE "terraform".
      Project   = "cam-counter"
      ManagedBy = "terraform"
      Env       = "prod"
      # Minúscula (F3): trazabilidad/limpieza de lo creado por el runner autónomo.
      project    = "cam-counter"
      managed_by = "mad-runner"
    }
  }
}

# Proveedor ALIAS `aws.iam` — para recursos IAM cuyas claves de tag son CASE-INSENSITIVE.
#
# PR03: AWS IAM `CreateRole` rechaza claves de tag que difieren sólo en mayúsculas
# (`Project`/`project`, `ManagedBy`/`managed_by`). El esquema F3 dual-case del proveedor por
# defecto —válido en S3/DynamoDB, que SÍ distinguen mayúsculas— rompería la creación de
# roles IAM. Este alias aplica un subconjunto IAM-safe que CONSERVA la clave MINÚSCULA
# `managed_by = "mad-runner"` (requisito de verificación F3) y `project = "cam-counter"`,
# más `Env` (sin colisión), y OMITE las capitalizadas `Project`/`ManagedBy` que colisionan.
# Sólo lo consumen los ROLES del módulo `iam_github_oidc`; el proveedor OIDC permanece en el
# proveedor por defecto con F3 completo (su API de tagging sí tolera dual-case).
provider "aws" {
  alias  = "iam"
  region = "us-east-1"

  allowed_account_ids = ["950639281773"]

  default_tags {
    tags = {
      Env        = "prod"
      project    = "cam-counter"
      managed_by = "mad-runner"
    }
  }
}
