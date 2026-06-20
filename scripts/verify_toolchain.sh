#!/usr/bin/env bash
#
# verify_toolchain.sh — Verificador de entorno de SÓLO LECTURA para cam-counter.
#
# Qué hace:
#   1. Comprueba SIEMPRE la presencia de la cadena de herramientas. La presencia es un
#      GATE DURO: si falta cualquier binario, termina con exit != 0.
#   2. Comprueba versiones mínimas razonables donde es fácil (node, python3, terraform).
#      Una versión por debajo del mínimo emite WARN pero NO cambia el código de salida.
#   3. SÓLO si hay credenciales AWS disponibles, asierta que la cuenta es 950639281773 y la
#      región efectiva us-east-1. Si NO hay credenciales, emite WARN y continúa (exit 0).
#
# Lo que NO hace:
#   - NO crea, modifica ni destruye NINGÚN recurso AWS. La única llamada AWS es
#     `aws sts get-caller-identity`, que es de sólo lectura.
#   - NO provisiona ni aplica infraestructura. Es seguro de correr en CI sin OIDC
#     (en ese caso: WARN + exit 0) y es idempotente.
#
set -euo pipefail

# --- Configuración esperada -------------------------------------------------------------
readonly EXPECTED_ACCOUNT="950639281773"
readonly EXPECTED_REGION="us-east-1"

# --- Salida con color (degradada si no hay TTY) -----------------------------------------
if [ -t 1 ]; then
  C_RED=$'\033[31m'; C_GRN=$'\033[32m'; C_YEL=$'\033[33m'; C_RST=$'\033[0m'
else
  C_RED=''; C_GRN=''; C_YEL=''; C_RST=''
fi

log_ok()   { printf '%sOK:%s   %s\n'   "$C_GRN" "$C_RST" "$*"; }
log_warn() { printf '%sWARN:%s %s\n'   "$C_YEL" "$C_RST" "$*"; }
log_err()  { printf '%sERROR:%s %s\n'  "$C_RED" "$C_RST" "$*" >&2; }

# --- Comparación de versiones (a >= b) usando orden natural -----------------------------
# Devuelve 0 (true) si $1 >= $2 según `sort -V`.
ver_ge() {
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -n1)" = "$2" ]
}

# Extrae el primer "X.Y(.Z)" de una cadena arbitraria de --version.
extract_ver() {
  printf '%s' "$1" | grep -oE '[0-9]+\.[0-9]+(\.[0-9]+)?' | head -n1
}

# --- 1) Presencia de binarios (GATE DURO) -----------------------------------------------
# Lista de herramientas requeridas SIEMPRE.
REQUIRED_BINS=(terraform aws node npm python3 jq gh java)
missing=()

for bin in "${REQUIRED_BINS[@]}"; do
  if command -v "$bin" >/dev/null 2>&1; then
    log_ok "presente: $bin ($("$bin" --version 2>&1 | head -n1))"
  else
    log_err "falta el binario requerido: $bin"
    missing+=("$bin")
  fi
done

# Gradle: aceptamos el binario `gradle` del sistema O el wrapper `gradlew` del repo.
repo_root="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
if command -v gradle >/dev/null 2>&1; then
  log_ok "presente: gradle ($(gradle --version 2>/dev/null | grep -iE '^Gradle' | head -n1))"
elif [ -x "$repo_root/rtsp-enable/gradlew" ]; then
  log_ok "presente: wrapper gradlew (rtsp-enable/gradlew)"
else
  log_err "falta gradle (ni binario 'gradle' ni wrapper 'rtsp-enable/gradlew')"
  missing+=("gradle")
fi

if [ "${#missing[@]}" -gt 0 ]; then
  log_err "faltan ${#missing[@]} herramienta(s): ${missing[*]}"
  log_err "la presencia de la toolchain es un requisito duro; abortando."
  exit 1
fi

# --- 2) Versiones mínimas (WARN, no bloqueante) -----------------------------------------
check_min_ver() {
  local name="$1" min="$2" raw="$3" got
  got="$(extract_ver "$raw")"
  if [ -z "$got" ]; then
    log_warn "no pude parsear la versión de $name (mínimo recomendado: $min); presencia OK"
    return 0
  fi
  if ver_ge "$got" "$min"; then
    log_ok "versión $name $got (>= $min)"
  else
    log_warn "versión $name $got < mínimo recomendado $min"
  fi
}

check_min_ver "node"      "18.0.0" "$(node --version 2>&1)"
check_min_ver "python3"   "3.10.0" "$(python3 --version 2>&1)"
check_min_ver "terraform" "1.5.0"  "$(terraform --version 2>&1 | head -n1)"

# --- 3) Identidad AWS (SÓLO si hay credenciales; de lo contrario WARN + exit 0) ----------
# Detección tolerante: intentamos sts get-caller-identity con un timeout corto. Si falla
# (sin credenciales, sin red o sin aws), omitimos la verificación y CONTINUAMOS con exit 0.
aws_caller() {
  # Sólo lectura. Timeout corto para no colgar CI sin OIDC.
  if command -v timeout >/dev/null 2>&1; then
    timeout 15 aws sts get-caller-identity --output json 2>/dev/null
  else
    aws sts get-caller-identity --cli-connect-timeout 5 --cli-read-timeout 10 \
      --output json 2>/dev/null
  fi
}

if ! command -v aws >/dev/null 2>&1; then
  log_warn "no hay 'aws' CLI: se omite la verificación de identidad AWS (continúo, exit 0)"
  exit 0
fi

identity_json=""
if identity_json="$(aws_caller)" && [ -n "$identity_json" ]; then
  account="$(printf '%s' "$identity_json" | jq -r '.Account // empty')"

  # Región efectiva: AWS_REGION > AWS_DEFAULT_REGION > `aws configure get region`.
  region="${AWS_REGION:-${AWS_DEFAULT_REGION:-}}"
  if [ -z "$region" ]; then
    region="$(aws configure get region 2>/dev/null || true)"
  fi

  fail=0
  if [ "$account" = "$EXPECTED_ACCOUNT" ]; then
    log_ok "cuenta AWS = $account (coincide con $EXPECTED_ACCOUNT)"
  else
    log_err "cuenta AWS = '${account:-<vacía>}'; se esperaba $EXPECTED_ACCOUNT"
    fail=1
  fi

  if [ "$region" = "$EXPECTED_REGION" ]; then
    log_ok "región efectiva = $region (coincide con $EXPECTED_REGION)"
  else
    log_err "región efectiva = '${region:-<vacía>}'; se esperaba $EXPECTED_REGION"
    fail=1
  fi

  if [ "$fail" -ne 0 ]; then
    log_err "la identidad AWS no coincide con la esperada; abortando."
    exit 2
  fi

  log_ok "identidad AWS verificada (sólo lectura; no se creó ningún recurso)."
else
  log_warn "sin credenciales AWS disponibles (o sin red): se omite la verificación de"
  log_warn "identidad. Esto es ESPERADO en CI sin OIDC. Continúo con exit 0."
fi

log_ok "verify_toolchain.sh completado."
exit 0
