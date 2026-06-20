#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# Bootstrap AUTÓNOMO en DOS FASES del backend de estado remoto de Terraform.
#
# Lo ejecuta el RUNNER MAD con las credenciales de SU ENTORNO (jamás commiteadas),
# NUNCA el CI ni un operador humano. Es IDEMPOTENTE: si el backend ya existe, se
# limita a inicializar contra el backend remoto y a comprobar que no hay cambios.
#
# F1 — Antes de cualquier apply se inspecciona el `plan`: si aparece CUALQUIER
#      destroy/replace de un recurso existente, se ABORTA.
# F2 — El apply autónomo pre-merge se restringe al módulo `state-backend` de este
#      PR (HCL curado); el CI permanece plan-only.
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROD="${HERE}/environments/prod"
BUCKET="cam-counter-tfstate-950639281773"
TABLE="cam-counter-tfstate-lock"
REGION="us-east-1"
PLAN_OUT="/tmp/pr02-bootstrap.plan"

echo "==> Bootstrap del backend de estado (bucket=${BUCKET}, tabla=${TABLE}, region=${REGION})"

backend_exists() {
  aws s3api head-bucket --bucket "${BUCKET}" >/dev/null 2>&1 &&
    aws dynamodb describe-table --table-name "${TABLE}" --region "${REGION}" >/dev/null 2>&1
}

assert_additive() {
  # F1: aborta si el plan contiene destroy/replace.
  if grep -Eq 'will be destroyed|must be replaced' "${PLAN_OUT}"; then
    echo "!! ABORTADO (F1): el plan contiene destroy/replace. NO se aplica." >&2
    exit 1
  fi
  echo "==> Plan ESTRICTAMENTE ADITIVO verificado (0 destroy / 0 replace)."
}

if backend_exists; then
  echo "==> El backend YA existe en AWS. Inicializando contra el backend remoto…"
  cp "${PROD}/backend.tf.example" "${PROD}/backend.tf"
  terraform -chdir="${PROD}" init -input=false -reconfigure
  echo "==> Comprobando idempotencia (plan -detailed-exitcode debe dar 0 = sin cambios)…"
  terraform -chdir="${PROD}" plan -input=false -detailed-exitcode
  echo "==> Backend ya operativo y sin cambios. Nada que hacer."
  exit 0
fi

echo "==> FASE 1 — estado LOCAL: crear el bucket de tfstate y la tabla de lock."
# Asegura que NO hay backend.tf activo (estado local) para esta primera fase.
rm -f "${PROD}/backend.tf"
terraform -chdir="${PROD}" init -input=false

echo "==> Inspección del plan (F1)…"
terraform -chdir="${PROD}" plan -input=false -no-color -out=tfplan.bootstrap | tee "${PLAN_OUT}"
assert_additive

echo "==> Aplicando (apply autónomo del runner)…"
terraform -chdir="${PROD}" apply -input=false tfplan.bootstrap
rm -f "${PROD}/tfplan.bootstrap"

echo "==> FASE 2 — migración: activar backend remoto y mover el state a S3."
cp "${PROD}/backend.tf.example" "${PROD}/backend.tf"
terraform -chdir="${PROD}" init -input=false -migrate-state -force-copy

echo "==> Comprobando idempotencia (plan -detailed-exitcode debe dar 0 = sin cambios)…"
terraform -chdir="${PROD}" plan -input=false -detailed-exitcode

echo "==> Bootstrap COMPLETADO. Backend remoto operativo y state migrado a S3."
