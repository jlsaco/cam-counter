# Requisitos de versión del módulo `amplify-app`.
#
# El módulo NO declara `provider` ni `backend`: los hereda de la raíz live
# (`terraform/environments/prod`). Aquí sólo se fijan las versiones mínimas, coherentes con
# la pila (provider AWS pineado `~> 5.x`, donde `aws_amplify_app.platform = "WEB_COMPUTE"`
# y `aws_amplify_branch.framework = "Next.js - SSR"` ya están soportados).
#
# UN solo proveedor `aws` (el por defecto): Amplify NO es IAM, así que sus claves de tag
# son CASE-SENSITIVE igual que S3/DynamoDB y el esquema F3 dual-case del proveedor por
# defecto es válido (no hay colisión `Project`/`project`). Por eso —a diferencia de
# iam-edge / cognito— este módulo NO necesita el proveedor IAM-safe `aws.iam`.
terraform {
  required_version = ">= 1.5.0"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}
