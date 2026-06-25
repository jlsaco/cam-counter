# Requisitos de versión del módulo `observability`.
# El módulo NO declara `provider` ni `backend`: los hereda de la raíz live
# (`terraform/environments/prod`). Recibe DOS proveedores: el por defecto (`aws`, dual-case
# F3, válido en CloudWatch/IoT/SNS/DynamoDB que distinguen mayúsculas) y el IAM-safe
# `aws.iam` (claves de tag case-insensitive) para el rol que asume la IoT Rule de presencia.
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source                = "hashicorp/aws"
      version               = "~> 5.0"
      configuration_aliases = [aws.iam]
    }
  }
}
