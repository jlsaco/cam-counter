# Requisitos de versión del módulo `state-backend`.
# El módulo NO declara `provider` ni `backend`: los hereda de la raíz live
# (`terraform/environments/prod`). Aquí sólo se fijan las versiones mínimas.
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
