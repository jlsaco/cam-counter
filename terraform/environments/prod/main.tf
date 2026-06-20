# Raíz live del ÚNICO entorno de producción de la pila de infra.
#
# Aquí se instancian los módulos de `terraform/modules/` y vive el ÚNICO state de
# producción compartido por toda la pila apilada (PR02→PR03→PR04→…→PR11), con backend
# S3 + lock DynamoDB (ver backend.tf / backend.tf.example).
#
# F1 — State aditivo y monótono: el runner sólo aplica desde la rama apilada MÁS ALTA
# con todo el HCL acumulado; nunca se reaplica una rama inferior tras una superior.
# F2 — Apply autónomo acotado: en PR02 el único módulo enumerado es `state-backend`.

module "state_backend" {
  source = "../../modules/state-backend"
  # Sin overrides: se usan los defaults del módulo (nombres reales del producto).
}
