# Módulo iam-github-oidc — restricciones de versión.
# NO declara `provider` ni `backend`: ambos se heredan de la raíz live
# (terraform/environments/prod). Así el módulo es reutilizable y testeable con
# `terraform init -backend=false` en CI sin credenciales.
#
# No requiere el provider `tls`: el thumbprint del proveedor OIDC se fija de forma
# ESTÁTICA (ver main.tf y README) para garantizar idempotencia/persistencia (F1)
# sin dependencias de red ni drift por rotación de certificados.
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
