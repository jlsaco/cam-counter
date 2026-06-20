# ─────────────────────────────────────────────────────────────────────────────
# Composición raíz del entorno `prod` (único entorno del producto).
#
# Mantiene el ÚNICO state de producción, ADITIVO Y MONÓTONO (F1), compartido por
# TODA la pila de PRs de infra. En PR02 sólo se instancia el backend de estado
# (bucket de tfstate + tabla de lock). Los PRs posteriores AÑADIRÁN módulos a este
# mismo state SIN destruir lo previo:
#   PR03 → provider OIDC + roles IAM (plan/deploy)
#   PR04 → bucket de media + tablas eventos/devices + IAM per-Pi
#   …
#   PR11 → bucket de releases OTA
#
# El runner MAD aplica SÓLO desde la rama apilada MÁS ALTA con todo el HCL
# acumulado; NUNCA reaplica esta rama (la más baja) una vez que un PR superior
# haya aplicado contra este mismo state. Ver README.md (F1).
# ─────────────────────────────────────────────────────────────────────────────

module "state_backend" {
  source = "../../modules/state-backend"
}
