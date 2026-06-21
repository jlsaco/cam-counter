# Requisitos de versión del módulo `iam-github-oidc`.
# El módulo NO declara `provider` ni `backend`: los hereda de la raíz live
# (`terraform/environments/prod`). Aquí sólo se fijan las versiones mínimas, coherentes
# con PR02 (provider AWS pineado `~> 5.x`).
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
