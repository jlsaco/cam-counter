# Módulo device-registry — restricciones de versión.
# NO declara `provider` ni `backend`: ambos se heredan de la raíz live
# (terraform/environments/prod). Reutilizable y testeable con
# `terraform init -backend=false` en CI sin credenciales.
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
