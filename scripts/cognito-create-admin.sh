#!/usr/bin/env bash
# cognito-create-admin.sh — alta del PRIMER operador admin en el User Pool de flota (WP10).
#
# Crea un usuario operador con AdminCreateUser, le fija una password PERMANENTE y lo añade al
# grupo de administradores (`cam-counter-admins`). Esto NO lo hace Terraform a propósito: la
# password NUNCA debe acabar en git ni en el tfstate. Aquí la password llega por ENV
# (`CAMCOUNTER_ADMIN_PASSWORD`) y sólo viaja a la API de Cognito.
#
# CERO SECRETOS: ni el email ni la password se commitean. El usuario queda CONFIRMED; en su
# primer login la Hosted UI / el flujo de auth le exigirá registrar el MFA TOTP (el pool tiene
# MFA ON), así que la password permanente NO debilita la postura: el segundo factor sigue siendo
# obligatorio.
#
# Requisitos: AWS CLI v2 autenticado (MAD lo ejecuta como el IAM admin `raspberry`, ~/.aws),
# Terraform con el state de `environments/prod` ya aplicado (para resolver el user-pool-id).
#
# Uso:
#   export CAMCOUNTER_ADMIN_EMAIL="ops@example.com"
#   export CAMCOUNTER_ADMIN_PASSWORD='…'        # NO en git; fuerte (>=12, may/min/núm/símbolo)
#   bash scripts/cognito-create-admin.sh
#
# Variables de entorno:
#   CAMCOUNTER_ADMIN_EMAIL     (obligatoria)  email/username del operador admin.
#   CAMCOUNTER_ADMIN_PASSWORD  (obligatoria)  password permanente; jamás se imprime ni commitea.
#   CAMCOUNTER_USER_POOL_ID    (opcional)     override; por defecto se lee del output de Terraform.
#   CAMCOUNTER_ADMINS_GROUP    (opcional)     grupo; default `cam-counter-admins`.
#   AWS_REGION                 (opcional)     default us-east-1.
set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
ADMINS_GROUP="${CAMCOUNTER_ADMINS_GROUP:-cam-counter-admins}"
TF_ENV_DIR="$(git rev-parse --show-toplevel)/terraform/environments/prod"

: "${CAMCOUNTER_ADMIN_EMAIL:?Define CAMCOUNTER_ADMIN_EMAIL (email/username del admin)}"
: "${CAMCOUNTER_ADMIN_PASSWORD:?Define CAMCOUNTER_ADMIN_PASSWORD (NO en git; password fuerte)}"

# Resolver el user-pool-id: override por ENV o desde el output de Terraform ya aplicado.
USER_POOL_ID="${CAMCOUNTER_USER_POOL_ID:-}"
if [[ -z "$USER_POOL_ID" ]]; then
  USER_POOL_ID="$(terraform -chdir="$TF_ENV_DIR" output -raw cognito_user_pool_id)"
fi
if [[ -z "$USER_POOL_ID" ]]; then
  echo "!! No se pudo resolver el user-pool-id (¿está aplicado el state de environments/prod?)." >&2
  exit 1
fi

echo ">> User Pool: $USER_POOL_ID  ·  admin: $CAMCOUNTER_ADMIN_EMAIL  ·  grupo: $ADMINS_GROUP"

# 1) AdminCreateUser idempotente: si el usuario ya existe, no es un error fatal.
#    SUPPRESS evita el email de invitación (no dependemos de SES); fijamos la password después.
if aws cognito-idp admin-get-user \
      --region "$REGION" --user-pool-id "$USER_POOL_ID" \
      --username "$CAMCOUNTER_ADMIN_EMAIL" >/dev/null 2>&1; then
  echo ">> El usuario ya existe; se omite AdminCreateUser (idempotente)."
else
  aws cognito-idp admin-create-user \
    --region "$REGION" --user-pool-id "$USER_POOL_ID" \
    --username "$CAMCOUNTER_ADMIN_EMAIL" \
    --user-attributes "Name=email,Value=$CAMCOUNTER_ADMIN_EMAIL" "Name=email_verified,Value=true" \
    --message-action SUPPRESS >/dev/null
  echo ">> Usuario creado (AdminCreateUser, invitación SUPPRESS)."
fi

# 2) Password PERMANENTE (sin estado FORCE_CHANGE_PASSWORD). El MFA TOTP del pool sigue siendo
#    obligatorio en el primer login, así que el segundo factor no se relaja.
aws cognito-idp admin-set-user-password \
  --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --username "$CAMCOUNTER_ADMIN_EMAIL" \
  --password "$CAMCOUNTER_ADMIN_PASSWORD" \
  --permanent
echo ">> Password permanente fijada (no impresa)."

# 3) Añadir al grupo de administradores (idempotente: re-añadir no falla).
aws cognito-idp admin-add-user-to-group \
  --region "$REGION" --user-pool-id "$USER_POOL_ID" \
  --username "$CAMCOUNTER_ADMIN_EMAIL" \
  --group-name "$ADMINS_GROUP"
echo ">> Añadido al grupo $ADMINS_GROUP."

echo ">> OK. Valida con: aws cognito-idp admin-get-user --region $REGION --user-pool-id $USER_POOL_ID --username $CAMCOUNTER_ADMIN_EMAIL"
