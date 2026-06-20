# Plantilla del backend S3 remoto del ÚNICO state de producción.
#
# Este fichero es una PLANTILLA. Se ACTIVA copiándolo a `backend.tf` SÓLO en la fase 2
# del bootstrap (ver README.md de este directorio):
#
#     cp backend.tf.example backend.tf
#     terraform -chdir=terraform/environments/prod init -migrate-state -force-copy
#
# Una vez migrado el state al bucket S3, se recomienda COMMITEAR `backend.tf` activo
# para que PR03+ usen el backend remoto sin pasos manuales. El backend NO contiene
# secretos: las credenciales las aporta el ENTORNO del runner (nunca el repo).

terraform {
  backend "s3" {
    bucket         = "cam-counter-tfstate-950639281773"
    key            = "environments/prod/terraform.tfstate"
    region         = "us-east-1"
    dynamodb_table = "cam-counter-tfstate-lock"
    encrypt        = true
  }
}
