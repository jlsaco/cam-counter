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
