# Configuración mínima de tflint para cam-counter.
# Sólo se ejecuta en CI (fmt-check / validate / tflint, plan-only, SIN credenciales
# AWS). Habilita el ruleset de Terraform (recomendado) y el plugin AWS.
config {
  call_module_type = "local"
}

plugin "terraform" {
  enabled = true
  preset  = "recommended"
}

plugin "aws" {
  enabled = true
  version = "0.41.0"
  source  = "github.com/terraform-linters/tflint-ruleset-aws"
}
