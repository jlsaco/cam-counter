# Configuración mínima de tflint con el plugin AWS.
# Se invoca con `tflint --chdir=<dir>`; el plugin se instala con `tflint --init`.

config {
  call_module_type = "all"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.44.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
