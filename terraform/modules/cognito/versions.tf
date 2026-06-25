# Requisitos de versión del módulo `cognito`.
# El módulo NO declara `provider` ni `backend`: los hereda de la raíz live
# (`terraform/environments/prod`). Aquí sólo se fijan las versiones mínimas, coherentes
# con la pila (provider AWS pineado `~> 5.x`).
#
# DOS proveedores AWS (configuration_aliases): los recursos de Cognito (case-sensitive en
# tags) usan el proveedor por defecto `aws` con el esquema F3 dual-case completo; el ROL IAM
# `authenticated` usa el proveedor IAM-safe `aws.iam` (claves de tag CASE-INSENSITIVE en IAM,
# igual que iam-edge / iam-github-oidc / iot-credential-provider). Mezclar Cognito + IAM en un
# mismo módulo obliga a recibir AMBOS proveedores para no romper `CreateRole` («Duplicate tag
# keys») ni perder los tags capitalizados en los recursos de Cognito.
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = "~> 5.0"
      configuration_aliases = [aws, aws.iam]
    }
  }
}
