#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# provision-device.sh — provisiona la identidad IoT Core de un Pi de la flota.
# ─────────────────────────────────────────────────────────────────────────────
#
# OBJETIVO (WP07): que un operador NO experto pueda dar de alta un dispositivo con
# UN comando. El script materializa SOLO lo POR-DEVICE y FUERA del state de
# Terraform (añadir un device nunca produce diff/destroy en el plan del runner MAD):
#
#   1. Genera llave privada + CSR en LOCAL (`openssl`). La llave privada NUNCA viaja
#      a AWS: a IoT sólo se envía el CSR. AWS firma el cert desde el CSR.
#   2. `aws iot create-certificate-from-csr --set-as-active` → cert X.509 del device.
#   3. `create-thing` con atributos (`site_id`, `device_id`, `camera_count`,
#      `release_channel`); el atributo `device_id` es el que la device-policy de WP06
#      usa en su variable de política para acotar los topics (ver más abajo).
#   4. `add-thing-to-thing-group` (grupo de sitio + grupo de canal).
#   5. `attach-policy cam-counter-device-policy` al CERT.
#   6. `attach-thing-principal` (vincula cert ↔ thing).
#   7. Conditional put del item en `cam-counter-devices` (idempotente; conforme al
#      contrato `contracts/device_registry_item.schema.json`).
#   8. Descarga `AmazonRootCA1.pem`.
#   9. Genera `.env` con SÓLO claves `CAMCOUNTER_*` (identidad + rutas + topics +
#      role alias + `CAMCOUNTER_SYNC_TRANSPORT=direct`). SIN credenciales AWS.
#  10. Empaqueta `device-bundle.tar.gz` (certs + `.env`) e imprime certificateArn +
#      checksum + recordatorio de canal seguro.
#
# DERIVACIÓN DEL TOPIC (nota del revisor, ALTA): los topics del `.env` siguen el canon
# de `docs/naming-standard.md` §3: `cam-counter/{device_id}/...`. La device-policy de
# WP06 acota el publish con la variable de política
# `cam-counter/${iot:Connection.Thing.Attributes[device_id]}/*`; como este script crea
# el Thing con el atributo `device_id={device}`, el topic del `.env` y el que concede la
# policy DERIVAN del mismo valor. Si no coincidieran, IoT denegaría el publish en
# silencio. Por eso el atributo `device_id` del Thing es OBLIGATORIO.
#
# GUARDARRAÍLES:
#   - La llave privada se genera local, `chmod 600`, y NUNCA se sube a AWS ni a git.
#   - El `.env` NO contiene `AWS_ACCESS_KEY_ID`/`AWS_SECRET_ACCESS_KEY`: la subida de
#     clips a S3 va por el role alias (WP04), no por llaves estáticas.
#   - Certs/llaves/bundles/.env quedan en `.gitignore` (jamás versionados).
#   - NO toca la identidad admin del runner (`raspberry` / `~/.aws`): usa las
#     credenciales del entorno tal cual.
#
# INFRA ESTABLE (WP06, Terraform): thing type, thing groups, device-policy y role alias
# son recursos de Terraform; este script NO los crea (los materializaría fuera del
# state y rompería el `terraform apply` de WP06). Si aún no están aplicados, el script
# AVISA y continúa con lo que sí puede hacer (thing + cert + registry); una RE-EJECUCIÓN
# idempotente completa los attach cuando WP06 esté aplicado.
#
# Idempotente: re-ejecutar con los mismos flags no duplica nada. Pasa `bash -n`.
#
# Uso:
#   scripts/provision-device.sh --site casa --device rpi-001 --camera 1 --channel stable
#   scripts/provision-device.sh --site casa --device rpi-001 --rotate
#   scripts/provision-device.sh --site casa --device rpi-001 --revoke
#   scripts/provision-device.sh --site casa --device rpi-001 --dry-run
# ─────────────────────────────────────────────────────────────────────────────
set -euo pipefail

# ───────────────────────── Canon de nombres (naming-standard.md) ──────────────
PRODUCT_PREFIX="cam-counter"
THING_TYPE_NAME="cam-counter-edge-device"      # §1 / §10 (3)
DEVICE_POLICY_NAME="cam-counter-device-policy" # §1 / §10 (5)
ROLE_ALIAS_NAME="cam-counter-edge-s3-role-alias" # §5 / §10 (6)
DEVICES_TABLE="cam-counter-devices"            # tabla device-registry (existente)
ROOT_CA_URL="https://www.amazontrust.com/repository/AmazonRootCA1.pem"
SLUG_REGEX='^[a-z0-9][a-z0-9-]{1,62}$'         # CLAUDE.md §3 / naming §0.3

# Rutas canónicas EN EL DEVICE (no en el repo) — naming-standard.md §2.
DEV_CERT_PATH="/etc/cam-counter/certs/device.cert.pem"
DEV_KEY_PATH="/etc/cam-counter/certs/device.private.key"
DEV_ROOTCA_PATH="/etc/cam-counter/certs/AmazonRootCA1.pem"

# ───────────────────────────────── Defaults ──────────────────────────────────
SITE_ID=""
DEVICE_ID=""
CAMERA_COUNT="1"
CHANNEL="stable"                               # default: stable (contrato: canary|stable)
MODE="provision"                               # provision | rotate | revoke
DRY_RUN="0"
REGION="${CAMCOUNTER_AWS_REGION:-us-east-1}"
# Directorio raíz de salida (bundles por device). Gitignored. NO en /etc en x86/dev.
OUT_ROOT="${CAMCOUNTER_PROVISION_OUT:-$(pwd)/provisioning}"

# ─────────────────────────────────── Util ────────────────────────────────────
say()  { printf '\033[1;34m[provision]\033[0m %s\n' "$*"; }
ok()   { printf '\033[1;32m[ ok ]\033[0m %s\n' "$*"; }
warn() { printf '\033[1;33m[warn]\033[0m %s\n' "$*" >&2; }
die()  { printf '\033[1;31m[fail]\033[0m %s\n' "$*" >&2; exit 1; }

usage() {
  sed -n '2,60p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

validate_slug() {
  # $1=valor $2=nombre-del-campo. Aplica el regex canónico a CADA slug componente
  # ANTES de componer cualquier nombre de recurso / key / topic (naming §0.3).
  local value="$1" field="$2"
  [[ "$value" =~ $SLUG_REGEX ]] || die "$field='$value' no cumple el slug canónico $SLUG_REGEX"
}

require_cmd() { command -v "$1" >/dev/null 2>&1 || die "falta la herramienta requerida: $1"; }

# Existencia de recursos de WP06 (no los creamos aquí). Devuelve 0 si existe.
iot_thing_type_exists() { aws iot describe-thing-type --region "$REGION" --thing-type-name "$1" >/dev/null 2>&1; }
iot_thing_group_exists(){ aws iot describe-thing-group --region "$REGION" --thing-group-name "$1" >/dev/null 2>&1; }
iot_policy_exists()     { aws iot get-policy --region "$REGION" --policy-name "$1" >/dev/null 2>&1; }
iot_thing_exists()      { aws iot describe-thing --region "$REGION" --thing-name "$1" >/dev/null 2>&1; }

# ──────────────────────────────── Parse flags ────────────────────────────────
while [[ $# -gt 0 ]]; do
  case "$1" in
    --site)    SITE_ID="${2:-}"; shift 2;;
    --device)  DEVICE_ID="${2:-}"; shift 2;;
    --camera)  CAMERA_COUNT="${2:-}"; shift 2;;
    --channel) CHANNEL="${2:-}"; shift 2;;
    --region)  REGION="${2:-}"; shift 2;;
    --out-dir) OUT_ROOT="${2:-}"; shift 2;;
    --rotate)  MODE="rotate"; shift;;
    --revoke)  MODE="revoke"; shift;;
    --dry-run) DRY_RUN="1"; shift;;
    -h|--help) usage 0;;
    *) die "flag desconocido: $1 (usa --help)";;
  esac
done

# ─────────────────────────── Validación de entrada ───────────────────────────
[[ -n "$SITE_ID"   ]] || die "falta --site (slug de sitio)"
[[ -n "$DEVICE_ID" ]] || die "falta --device (slug de dispositivo)"
validate_slug "$SITE_ID"   "site_id"
validate_slug "$DEVICE_ID" "device_id"
validate_slug "$CHANNEL"   "channel"
# El canal debe estar en el enum del contrato device_registry_item (canary|stable):
# escribir un item con un canal inválido violaría el contrato y el GSI de canal.
case "$CHANNEL" in
  canary|stable) :;;
  *) die "channel='$CHANNEL' inválido. El contrato device_registry_item sólo admite: canary|stable";;
esac
[[ "$CAMERA_COUNT" =~ ^[1-9][0-9]*$ ]] || die "--camera debe ser un entero >=1 (nº de cámaras lógicas)"

# Nombres COMPUESTOS (pueden exceder 63 chars; el regex valida los slugs, no el compuesto).
THING_NAME="${PRODUCT_PREFIX}-${SITE_ID}-${DEVICE_ID}"        # == MQTT client-id (§1)
SITE_GROUP="${PRODUCT_PREFIX}-site-${SITE_ID}"                # §1
CHANNEL_GROUP="${PRODUCT_PREFIX}-channel-${CHANNEL}"          # §1
TOPIC_PREFIX="${PRODUCT_PREFIX}/${DEVICE_ID}"                 # §3 (identidad = device_id)

# camera_ids globales únicos '{device_id}-cam{N}' (CLAUDE.md §3) — validar cada uno.
CAMERA_IDS=()
for ((n=1; n<=CAMERA_COUNT; n++)); do
  cam_id="${DEVICE_ID}-cam${n}"
  validate_slug "$cam_id" "camera_id"
  CAMERA_IDS+=("$cam_id")
done

OUT_DIR="${OUT_ROOT}/${THING_NAME}"
CERTS_DIR="${OUT_DIR}/certs"
STATE_FILE="${OUT_DIR}/.provision-state"   # KEY=VALUE: CERTIFICATE_ID / CERTIFICATE_ARN
KEY_FILE="${CERTS_DIR}/device.private.key"
CSR_FILE="${CERTS_DIR}/device.csr"
CERT_FILE="${CERTS_DIR}/device.cert.pem"
ROOTCA_FILE="${CERTS_DIR}/AmazonRootCA1.pem"
ENV_FILE="${OUT_DIR}/.env"
BUNDLE_FILE="${OUT_DIR}/device-bundle.tar.gz"

# ─────────────────────────────── .env (render) ───────────────────────────────
# SÓLO claves CAMCOUNTER_*; NUNCA credenciales AWS. Topics derivados del device_id
# (== atributo del Thing, mismo canon que la device-policy de WP06).
render_env() {
  local endpoint="$1"
  cat <<EOF
# === cam-counter — .env de dispositivo (generado por scripts/provision-device.sh) ===
# Identidad IoT Core del Pi '${THING_NAME}'. SÓLO claves CAMCOUNTER_*.
# NO contiene credenciales AWS: la subida de clips a S3 usa el role alias
# (IoT Credentials Provider, WP04), nunca llaves estáticas.
# Este fichero contiene rutas a la llave privada del device: trátalo como secreto.

CAMCOUNTER_SITE_ID=${SITE_ID}
CAMCOUNTER_DEVICE_ID=${DEVICE_ID}
CAMCOUNTER_CAMERA_COUNT=${CAMERA_COUNT}
CAMCOUNTER_AWS_REGION=${REGION}

# --- Identidad IoT Core (mTLS) ---
CAMCOUNTER_IOT_THING_NAME=${THING_NAME}
CAMCOUNTER_IOT_CLIENT_ID=${THING_NAME}
CAMCOUNTER_IOT_ENDPOINT=${endpoint}
CAMCOUNTER_IOT_CERT_PATH=${DEV_CERT_PATH}
CAMCOUNTER_IOT_KEY_PATH=${DEV_KEY_PATH}
CAMCOUNTER_IOT_ROOT_CA_PATH=${DEV_ROOTCA_PATH}

# --- Role alias (IoT Credentials Provider -> STS de corta vida para S3) ---
CAMCOUNTER_ROLE_ALIAS=${ROLE_ALIAS_NAME}

# --- Canal OTA (espejo del thing-group de canal) ---
CAMCOUNTER_RELEASE_CHANNEL=${CHANNEL}

# --- Topics MQTT (derivados del device_id == atributo del Thing; mismo canon que la
#     device-policy de WP06: cam-counter/\${iot:Connection.Thing.Attributes[device_id]}/*) ---
CAMCOUNTER_IOT_TOPIC_PREFIX=${TOPIC_PREFIX}
CAMCOUNTER_IOT_TOPIC_EVENTS=${TOPIC_PREFIX}/events/crossing
CAMCOUNTER_IOT_TOPIC_STATUS=${TOPIC_PREFIX}/status
CAMCOUNTER_IOT_TOPIC_TELEMETRY=${TOPIC_PREFIX}/telemetry
CAMCOUNTER_IOT_TOPIC_CMD=${TOPIC_PREFIX}/cmd

# --- Transporte de sync edge->cloud. 'direct' = camino actual (boto3 directo, sin
#     cambios). 'iot' = publicar por MQTT a IoT Core; se habilita en WPs posteriores. ---
CAMCOUNTER_SYNC_TRANSPORT=direct
EOF
}

# ─────────────────────────── Item del device-registry ────────────────────────
# Conforme a contracts/device_registry_item.schema.json. PK=DEVICE#{device_id};
# GSI1PK=CHANNEL#{channel}, GSI1SK=DEVICE#{device_id} (device-registry module).
registry_item_json() {
  local cams="" first=1
  for c in "${CAMERA_IDS[@]}"; do
    [[ $first -eq 1 ]] && first=0 || cams+=","
    cams+="{\"S\":\"${c}\"}"
  done
  cat <<EOF
{
  "PK": {"S": "DEVICE#${DEVICE_ID}"},
  "GSI1PK": {"S": "CHANNEL#${CHANNEL}"},
  "GSI1SK": {"S": "DEVICE#${DEVICE_ID}"},
  "device_id": {"S": "${DEVICE_ID}"},
  "site_id": {"S": "${SITE_ID}"},
  "camera_ids": {"L": [${cams}]},
  "release_channel": {"S": "${CHANNEL}"},
  "status": {"S": "offline"},
  "last_update_status": {"S": "idle"},
  "schema_version": {"N": "1"}
}
EOF
}

# ───────────────────────────────── Dry-run ───────────────────────────────────
# Valida + computa nombres + renderiza el .env e ítem de registry SIN tocar AWS,
# openssl ni la red. Útil en CI x86 sin credenciales.
if [[ "$DRY_RUN" == "1" ]]; then
  say "DRY-RUN (no se llama a AWS / openssl / red)"
  cat <<EOF
  modo            : ${MODE}
  thing_name      : ${THING_NAME}
  thing_type      : ${THING_TYPE_NAME}
  site_group      : ${SITE_GROUP}
  channel_group   : ${CHANNEL_GROUP}
  device_policy   : ${DEVICE_POLICY_NAME}
  role_alias      : ${ROLE_ALIAS_NAME}
  topic_prefix    : ${TOPIC_PREFIX}
  camera_ids      : ${CAMERA_IDS[*]}
  out_dir         : ${OUT_DIR}
EOF
  echo "--- .env ---"
  render_env "<iot-ats-endpoint>"
  echo "--- device-registry item ---"
  registry_item_json
  exit 0
fi

# ──────────────────────────── Preflight (AWS) ────────────────────────────────
require_cmd aws
caller="$(aws sts get-caller-identity --query 'Arn' --output text 2>/dev/null)" \
  || die "no hay credenciales AWS válidas en el entorno (no toco ~/.aws)"
say "identidad AWS: ${caller} | región: ${REGION}"

mkdir -p "$CERTS_DIR"
chmod 700 "$OUT_DIR" "$CERTS_DIR" 2>/dev/null || true
[[ -f "$STATE_FILE" ]] && source "$STATE_FILE" || true
CERTIFICATE_ID="${CERTIFICATE_ID:-}"
CERTIFICATE_ARN="${CERTIFICATE_ARN:-}"

write_state() {
  umask 077
  { echo "CERTIFICATE_ID=${CERTIFICATE_ID}"; echo "CERTIFICATE_ARN=${CERTIFICATE_ARN}"; } > "$STATE_FILE"
}

# Descubre el cert vinculado al thing si no hay state local (operador sin el dir).
discover_cert_from_thing() {
  iot_thing_exists "$THING_NAME" || return 1
  local arn
  arn="$(aws iot list-thing-principals --region "$REGION" --thing-name "$THING_NAME" \
        --query 'principals[0]' --output text 2>/dev/null || true)"
  [[ -n "$arn" && "$arn" != "None" ]] || return 1
  CERTIFICATE_ARN="$arn"
  CERTIFICATE_ID="${arn##*/}"
  return 0
}

# ───────────────────── openssl: llave + CSR (LOCAL, sin AWS) ──────────────────
gen_key_and_csr() {
  require_cmd openssl
  umask 077
  say "generando llave privada RSA-2048 + CSR en LOCAL (la llave NUNCA viaja a AWS)"
  openssl genrsa -out "$KEY_FILE" 2048 >/dev/null 2>&1
  chmod 600 "$KEY_FILE"
  # CN = thing name (== client-id). El CSR no lleva secretos; sólo la clave pública.
  openssl req -new -key "$KEY_FILE" -out "$CSR_FILE" -subj "/CN=${THING_NAME}" >/dev/null 2>&1
  ok "llave privada: ${KEY_FILE} (chmod 600, local)"
}

# crea cert desde CSR y lo deja ACTIVO; rellena CERTIFICATE_ID/ARN y escribe el PEM.
create_cert_from_csr() {
  require_cmd python3
  say "creando certificado desde el CSR (--set-as-active)"
  local resp_file="${OUT_DIR}/.create-cert.json"
  umask 077
  # La respuesta lleva certificateArn/Id y el PEM (la llave privada ya está LOCAL).
  aws iot create-certificate-from-csr --region "$REGION" \
    --certificate-signing-request "file://${CSR_FILE}" --set-as-active \
    --output json > "$resp_file"
  # Extrae arn/id/pem con python3 (el PEM es multilínea: nada de sed/grep frágil).
  CERTIFICATE_ARN="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["certificateArn"])' "$resp_file")"
  CERTIFICATE_ID="$(python3 -c 'import json,sys; print(json.load(open(sys.argv[1]))["certificateId"])' "$resp_file")"
  python3 -c 'import json,sys; open(sys.argv[2],"w").write(json.load(open(sys.argv[1]))["certificatePem"])' "$resp_file" "$CERT_FILE"
  rm -f "$resp_file"
  [[ -n "$CERTIFICATE_ARN" && -n "$CERTIFICATE_ID" ]] || die "no se pudo crear el certificado"
  chmod 600 "$CERT_FILE"
  write_state
  ok "cert creado: ${CERTIFICATE_ARN}"
}

# ─────────────────────── Thing + atributos (idempotente) ─────────────────────
ensure_thing() {
  local attr_payload
  # El atributo device_id es OBLIGATORIO: la device-policy de WP06 lo usa en su
  # variable de política para acotar topics (ver cabecera). site_id/camera_count/
  # release_channel son observabilidad.
  attr_payload="$(cat <<EOF
{"attributes":{"site_id":"${SITE_ID}","device_id":"${DEVICE_ID}","camera_count":"${CAMERA_COUNT}","release_channel":"${CHANNEL}"}}
EOF
)"
  local type_args=()
  if iot_thing_type_exists "$THING_TYPE_NAME"; then
    type_args=(--thing-type-name "$THING_TYPE_NAME")
  else
    warn "thing type '${THING_TYPE_NAME}' no existe aún (WP06 no aplicado): creo el Thing SIN type; re-ejecuta tras aplicar WP06 para asociarlo."
  fi
  if iot_thing_exists "$THING_NAME"; then
    say "Thing '${THING_NAME}' ya existe → update-thing (atributos)"
    aws iot update-thing --region "$REGION" --thing-name "$THING_NAME" \
      "${type_args[@]}" --attribute-payload "$attr_payload" >/dev/null
  else
    say "creando Thing '${THING_NAME}' con atributos"
    aws iot create-thing --region "$REGION" --thing-name "$THING_NAME" \
      "${type_args[@]}" --attribute-payload "$attr_payload" >/dev/null
  fi
  ok "Thing listo: ${THING_NAME}"
}

# add-thing-to-thing-group es idempotente (re-añadir = no-op). Sólo si el grupo existe.
ensure_group() {
  local group="$1"
  if iot_thing_group_exists "$group"; then
    aws iot add-thing-to-thing-group --region "$REGION" \
      --thing-name "$THING_NAME" --thing-group-name "$group" >/dev/null
    ok "Thing en grupo: ${group}"
  else
    warn "thing-group '${group}' no existe aún (WP06 no aplicado): se omite; re-ejecuta tras aplicar WP06."
  fi
}

# attach-policy al CERT (idempotente). Sólo si la policy existe (WP06).
ensure_policy_attached() {
  if iot_policy_exists "$DEVICE_POLICY_NAME"; then
    aws iot attach-policy --region "$REGION" \
      --policy-name "$DEVICE_POLICY_NAME" --target "$CERTIFICATE_ARN" >/dev/null
    ok "policy '${DEVICE_POLICY_NAME}' adjunta al cert"
  else
    warn "device-policy '${DEVICE_POLICY_NAME}' no existe aún (WP06 no aplicado): el cert NO podrá conectarse hasta adjuntarla. Re-ejecuta tras aplicar WP06."
  fi
}

# vincula cert ↔ thing (idempotente: re-vincular = no-op).
ensure_thing_principal() {
  aws iot attach-thing-principal --region "$REGION" \
    --thing-name "$THING_NAME" --principal "$CERTIFICATE_ARN" >/dev/null
  ok "cert vinculado al Thing (attach-thing-principal)"
}

# conditional put: no sobreescribe un item existente (idempotente). El contrato y el
# heartbeat del device (reported_version/status) son del Pi; este script es el escritor
# AUTORITATIVO en bootstrap, el hook devices-register (WP08) hace upsert tolerante.
ensure_registry_item() {
  local item_file="${OUT_DIR}/.registry-item.json"
  umask 077
  registry_item_json > "$item_file"
  if aws dynamodb put-item --region "$REGION" --table-name "$DEVICES_TABLE" \
       --item "file://${item_file}" \
       --condition-expression "attribute_not_exists(PK)" >/dev/null 2>&1; then
    ok "item de device registrado en ${DEVICES_TABLE} (nuevo)"
  else
    # ConditionalCheckFailed => ya existe: idempotente, no pisamos el heartbeat del Pi.
    say "item de device ya existía en ${DEVICES_TABLE} (idempotente; no se sobreescribe)"
  fi
  rm -f "$item_file"
}

download_root_ca() {
  require_cmd curl
  say "descargando Amazon Root CA 1"
  curl -fsSL "$ROOT_CA_URL" -o "$ROOTCA_FILE" || die "no se pudo descargar la Root CA"
  ok "Root CA: ${ROOTCA_FILE}"
}

iot_ats_endpoint() {
  aws iot describe-endpoint --region "$REGION" --endpoint-type iot:Data-ATS \
    --query 'endpointAddress' --output text
}

build_bundle() {
  local endpoint="$1"
  umask 077
  render_env "$endpoint" > "$ENV_FILE"
  # Bundle con layout listo para el device: certs/ + .env.
  tar -czf "$BUNDLE_FILE" -C "$OUT_DIR" \
    certs/device.cert.pem certs/device.private.key certs/AmazonRootCA1.pem .env
  chmod 600 "$BUNDLE_FILE" "$ENV_FILE"
  ok "bundle: ${BUNDLE_FILE}"
}

# ───────────────────────────────── Flujos ────────────────────────────────────
do_provision() {
  # 1) cert: reusar si ya hay uno ACTIVO en el state (idempotente); si no, crear.
  local reuse=0
  if [[ -n "$CERTIFICATE_ID" ]] && \
     aws iot describe-certificate --region "$REGION" --certificate-id "$CERTIFICATE_ID" \
       --query 'certificateDescription.status' --output text 2>/dev/null | grep -qx ACTIVE; then
    say "reusando cert ACTIVO existente: ${CERTIFICATE_ARN}"
    reuse=1
  fi
  if [[ "$reuse" -eq 0 ]]; then
    gen_key_and_csr
    create_cert_from_csr
  fi
  # 2..7) thing, grupos, policy, principal, registry.
  ensure_thing
  ensure_group "$SITE_GROUP"
  ensure_group "$CHANNEL_GROUP"
  ensure_policy_attached
  ensure_thing_principal
  ensure_registry_item
  # 8..10) Root CA, .env, bundle.
  download_root_ca
  local endpoint; endpoint="$(iot_ats_endpoint)"
  build_bundle "$endpoint"

  # Resumen.
  local sum; sum="$(sha256sum "$BUNDLE_FILE" | awk '{print $1}')"
  echo
  ok   "device aprovisionado: ${THING_NAME}"
  say  "certificateArn : ${CERTIFICATE_ARN}"
  say  "bundle         : ${BUNDLE_FILE}"
  say  "sha256(bundle) : ${sum}"
  say  "canal OTA      : ${CHANNEL}"
  warn "Transfiere el bundle al device por un CANAL SEGURO (scp/USB). Contiene la"
  warn "llave PRIVADA del device: NUNCA por email/chat ni a git. En el device va a"
  warn "/etc/cam-counter/certs/ (cert+key chmod 600) y el .env a la config del servicio."
}

do_rotate() {
  iot_thing_exists "$THING_NAME" || die "no existe el Thing '${THING_NAME}': aprovisiona primero (sin --rotate)"
  local old_id="$CERTIFICATE_ID" old_arn="$CERTIFICATE_ARN"
  say "rotando el certificado del Thing '${THING_NAME}'"
  # 1) Nuevo par llave+CSR y cert ACTIVO. attach policy + principal al NUEVO cert.
  gen_key_and_csr
  create_cert_from_csr
  ensure_policy_attached
  ensure_thing_principal
  # 2) El cert viejo: detach + INACTIVE (no se borra; permite rollback dentro de retención).
  if [[ -n "$old_arn" && "$old_arn" != "$CERTIFICATE_ARN" ]]; then
    say "desactivando el cert anterior: ${old_arn}"
    iot_policy_exists "$DEVICE_POLICY_NAME" && \
      aws iot detach-policy --region "$REGION" --policy-name "$DEVICE_POLICY_NAME" --target "$old_arn" >/dev/null 2>&1 || true
    aws iot detach-thing-principal --region "$REGION" --thing-name "$THING_NAME" --principal "$old_arn" >/dev/null 2>&1 || true
    aws iot update-certificate --region "$REGION" --certificate-id "$old_id" --new-status INACTIVE >/dev/null 2>&1 || true
    ok "cert anterior desactivado (INACTIVE, detached)"
  fi
  # 3) Re-empaquetar bundle con el nuevo cert/llave.
  download_root_ca
  local endpoint; endpoint="$(iot_ats_endpoint)"
  build_bundle "$endpoint"
  local sum; sum="$(sha256sum "$BUNDLE_FILE" | awk '{print $1}')"
  echo
  ok  "rotación completa. NUEVO certificateArn: ${CERTIFICATE_ARN}"
  say "bundle: ${BUNDLE_FILE} | sha256: ${sum}"
  warn "Transfiere el NUEVO bundle al device por canal seguro y reinicia el servicio."
}

do_revoke() {
  # Localiza el cert activo (state local o, en su defecto, vía el Thing).
  if [[ -z "$CERTIFICATE_ARN" ]]; then
    discover_cert_from_thing || die "no encuentro cert vinculado a '${THING_NAME}' (ni state ni thing-principal)"
  fi
  say "REVOCANDO el certificado de '${THING_NAME}': ${CERTIFICATE_ARN}"
  iot_policy_exists "$DEVICE_POLICY_NAME" && \
    aws iot detach-policy --region "$REGION" --policy-name "$DEVICE_POLICY_NAME" --target "$CERTIFICATE_ARN" >/dev/null 2>&1 || true
  aws iot detach-thing-principal --region "$REGION" --thing-name "$THING_NAME" --principal "$CERTIFICATE_ARN" >/dev/null 2>&1 || true
  aws iot update-certificate --region "$REGION" --certificate-id "$CERTIFICATE_ID" --new-status REVOKED >/dev/null \
    || die "no se pudo marcar el cert como REVOKED"
  aws iot delete-certificate --region "$REGION" --certificate-id "$CERTIFICATE_ID" --force-delete >/dev/null 2>&1 \
    && ok "cert borrado de IoT" || warn "cert REVOKED pero no borrado (puede tener principals/policies residuales)"
  # Limpia el state local (el bundle local queda inválido).
  CERTIFICATE_ID=""; CERTIFICATE_ARN=""; write_state
  echo
  ok  "certificado revocado para '${THING_NAME}'."
  warn "El Thing y su item de registry SIGUEN existiendo. Para re-emitir credenciales:"
  warn "  scripts/provision-device.sh --site ${SITE_ID} --device ${DEVICE_ID} --channel ${CHANNEL}"
}

case "$MODE" in
  provision) do_provision;;
  rotate)    do_rotate;;
  revoke)    do_revoke;;
  *) die "modo desconocido: $MODE";;
esac
