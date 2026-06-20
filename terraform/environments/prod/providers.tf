provider "aws" {
  region = "us-east-1"

  # Salvaguarda: aborta si las credenciales del entorno apuntan a otra cuenta.
  allowed_account_ids = ["950639281773"]

  # ───────────────────────────────────────────────────────────────────────────
  # F3 — Tags unificados, aplicados a TODOS los recursos de esta raíz (y de sus
  # módulos) vía default_tags:
  #
  #   - Claves CAPITALIZADAS (inventario estándar):
  #       Project   = "cam-counter"
  #       ManagedBy = "terraform"   ← SIEMPRE "terraform"; NUNCA "mad-runner".
  #       Env       = "prod"
  #   - Claves en MINÚSCULA (trazabilidad/limpieza de lo que crea el runner):
  #       project    = "cam-counter"
  #       managed_by = "mad-runner"
  #
  # La verificación de `managed_by=mad-runner` busca la clave en MINÚSCULA.
  # ───────────────────────────────────────────────────────────────────────────
  default_tags {
    tags = {
      Project    = "cam-counter"
      ManagedBy  = "terraform"
      Env        = "prod"
      project    = "cam-counter"
      managed_by = "mad-runner"
    }
  }
}
