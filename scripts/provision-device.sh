#!/usr/bin/env bash
# provision-device.sh — provisiona (o rota/revoca) la identidad IoT de UN Pi de la flota.
#
# Materializa lo PER-DEVICE *fuera* del state de Terraform: llave+CSR locales, certificado
# X.509 desde el CSR, Thing + atributos, alta en grupos (sitio + canal), attach de la policy
# de dispositivo al cert, alta del item en `cam-counter-devices` (conforme al contrato) y un
# `device-bundle.tar.gz` con los certs + un `.env` de claves `CAMCOUNTER_*` (SIN credenciales
# AWS). Agregar/rotar un device NUNCA produce diff/destroy en el plan de Terraform de MAD: la
# infra estable (thing type, grupos, policy, role-alias) la crea el módulo `iot-core` (WP06).
#
# GARANTÍAS:
#   - La LLAVE PRIVADA se genera en local con `openssl` y NUNCA viaja a AWS (solo el CSR).
#   - El `.env` NO contiene AWS_ACCESS_KEY_ID/SECRET: la subida de clips va por el role-alias.
#   - Idempotente: re-ejecutar no duplica certs ni el item de registry.
#   - Slugs validados (`^[a-z0-9][a-z0-9-]{1,62}$`) ANTES de componer nombres/keys/topics.
#
# Uso:
#   scripts/provision-device.sh --site <slug> --device <slug> [--camera N] [--channel stable|canary]
#   scripts/provision-device.sh --site <slug> --device <slug> --rotate
#   scripts/provision-device.sh --site <slug> --device <slug> --revoke
#   scripts/provision-device.sh --site <slug> --device <slug> --dry-run   # solo local, sin AWS
#
# Requiere: bash, openssl, jq, aws-cli v2 con credenciales del operador (NO las del runner MAD).
# Pasa `bash -n` y `shellcheck`.
set -euo pipefail

# ───────────────────────── Constantes canónicas (docs/naming-standard.md) ─────────────────────────
readonly PRODUCT_PREFIX="cam-counter"
readonly THING_TYPE_NAME="cam-counter-edge-device"          # §1
readonly DEVICE_POLICY_NAME="cam-counter-device-policy"     # §1 / §10 (#5)
readonly ROLE_ALIAS_NAME="cam-counter-edge-s3-role-alias"   # §5 / §10 (#6)
readonly DEVICES_TABLE="cam-counter-devices"                # §12 (existente)
readonly SLUG_RE='^[a-z0-9][a-z0-9-]{1,62}$'                # Apéndice naming-standard / CLAUDE.md §3
readonly SCHEMA_VERSION=1                                   # contracts/device_registry_item.schema.json
readonly ROOT_CA_URL="https://www.amazontrust.com/repository/AmazonRootCA1.pem"

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
readonly OUT_ROOT="${REPO_ROOT}/out/provisioning"          # gitignored — certs/llaves/bundles JAMÁS en git

# ───────────────────────────────────── Flags / defaults ─────────────────────────────────────
SITE_ID=""
DEVICE_ID=""
CAMERA_COUNT=1
CHANNEL="stable"                                           # default; enum válido del contrato: stable|canary
MODE="provision"                                           # provision | rotate | revoke
DRY_RUN=0
AWS_REGION="${CAMCOUNTER_AWS_REGION:-us-east-1}"

die()  { printf '\033[31mERROR:\033[0m %s\n' "$*" >&2; exit 1; }
log()  { printf '\033[36m• %s\033[0m\n' "$*" >&2; }
ok()   { printf '\033[32m✓ %s\033[0m\n' "$*" >&2; }
warn() { printf '\033[33m! %s\033[0m\n' "$*" >&2; }

usage() {
  sed -n '2,32p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# ───────────────────────────────────── Parseo de argumentos ─────────────────────────────────────
while [ $# -gt 0 ]; do
  case "$1" in
    --site)    SITE_ID="${2:-}";    shift 2 ;;
    --device)  DEVICE_ID="${2:-}";  shift 2 ;;
    --camera)  CAMERA_COUNT="${2:-}"; shift 2 ;;
    --channel) CHANNEL="${2:-}";    shift 2 ;;
    --rotate)  MODE="rotate";       shift ;;
    --revoke)  MODE="revoke";       shift ;;
    --dry-run) DRY_RUN=1;           shift ;;
    -h|--help) usage 0 ;;
    *) die "flag desconocido: $1 (usa --help)" ;;
  esac
done

# ─────────────────────────────────────── Validación ───────────────────────────────────────
[ -n "$SITE_ID" ]   || die "falta --site <slug>"
[ -n "$DEVICE_ID" ] || die "falta --device <slug>"
[[ "$SITE_ID"   =~ $SLUG_RE ]] || die "site_id inválido '$SITE_ID' (regex $SLUG_RE)"
[[ "$DEVICE_ID" =~ $SLUG_RE ]] || die "device_id inválido '$DEVICE_ID' (regex $SLUG_RE)"
[[ "$CHANNEL"   =~ $SLUG_RE ]] || die "channel inválido '$CHANNEL' (regex $SLUG_RE)"
# El canal DEBE ser un valor válido del contrato device_registry_item (enum: canary|stable).
case "$CHANNEL" in
  stable|canary) : ;;
  *) die "channel '$CHANNEL' no válido: el contrato device_registry_item solo admite 'stable' o 'canary'" ;;
esac
[[ "$CAMERA_COUNT" =~ ^[1-9][0-9]*$ ]] || die "--camera debe ser un entero positivo (nº de cámaras), no '$CAMERA_COUNT'"
[ "$MODE" != "rotate" ] || [ "$DRY_RUN" -eq 0 ] || die "--rotate y --dry-run son incompatibles"
[ "$MODE" != "revoke" ] || [ "$DRY_RUN" -eq 0 ] || die "--revoke y --dry-run son incompatibles"

for bin in openssl jq aws; do command -v "$bin" >/dev/null 2>&1 || die "falta el binario requerido: $bin"; done

# ─────────────────────── Nombres derivados (mismo canon que la policy de WP06) ───────────────────────
# Thing name = client-id MQTT (naming-standard §1). El device_id se fija como ATRIBUTO del Thing
# para que la policy de WP06 (`${iot:Connection.Thing.Attributes[device_id]}`) autorice el publish
# sobre `cam-counter/{device_id}/...` — el MISMO canon de topic que el `.env` que generamos (§3).
THING_NAME="${PRODUCT_PREFIX}-${SITE_ID}-${DEVICE_ID}"
SITE_GROUP="${PRODUCT_PREFIX}-site-${SITE_ID}"             # §1 grupo por sitio
CHANNEL_GROUP="${PRODUCT_PREFIX}-channel-${CHANNEL}"       # §1 grupo por canal OTA (la "flota" segmentada)
TOPIC_BASE="${PRODUCT_PREFIX}/${DEVICE_ID}"                # §3 raíz de topics (deriva del device_id)

# camera_ids = '{device_id}-cam{N}' (CLAUDE.md §3). Se validan como slugs y se persisten en el item.
CAMERA_IDS=()
for n in $(seq 1 "$CAMERA_COUNT"); do
  cid="${DEVICE_ID}-cam${n}"
  [[ "$cid" =~ $SLUG_RE ]] || die "camera_id derivado inválido '$cid'"
  CAMERA_IDS+=("$cid")
done

OUT_DIR="${OUT_ROOT}/${THING_NAME}"
CERTS_DIR="${OUT_DIR}/certs"
KEY_PATH="${CERTS_DIR}/device.private.key"
CSR_PATH="${CERTS_DIR}/device.csr.pem"
CERT_PATH="${CERTS_DIR}/device.cert.pem"
ROOT_CA_PATH="${CERTS_DIR}/AmazonRootCA1.pem"
ENV_PATH="${OUT_DIR}/.env"
BUNDLE_PATH="${OUT_DIR}/device-bundle.tar.gz"
CERTID_PATH="${OUT_DIR}/.certificate-id"                   # rastro local del cert vigente (para --rotate)

aws_iot() { aws iot "$@" --region "$AWS_REGION" --output json; }

# ─────────────────────────── Endpoint IoT ATS (no secreto) ───────────────────────────
iot_endpoint() {
  if [ "$DRY_RUN" -eq 1 ]; then echo "DRY-RUN-ENDPOINT-ats.iot.${AWS_REGION}.amazonaws.com"; return; fi
  aws_iot describe-endpoint --endpoint-type iot:Data-ATS | jq -r '.endpointAddress'
}

# ───────────── Llave privada + CSR LOCALES (la llave NUNCA sale del host) ─────────────
generate_key_and_csr() {
  mkdir -p "$CERTS_DIR"
  if [ -f "$KEY_PATH" ] && [ "$MODE" != "rotate" ]; then
    log "llave privada ya existe (reuso): $KEY_PATH"
  else
    log "generando llave privada RSA-2048 local (NUNCA viaja a AWS)"
    openssl genrsa -out "$KEY_PATH" 2048 2>/dev/null
    chmod 600 "$KEY_PATH"
  fi
  # CN = thing name (trazabilidad); el CSR es lo único que se envía a AWS.
  openssl req -new -key "$KEY_PATH" -out "$CSR_PATH" -subj "/CN=${THING_NAME}/O=cam-counter" 2>/dev/null
  chmod 600 "$KEY_PATH"
  ok "llave+CSR locales generados ($CERTS_DIR)"
}

# ───────────────────────────── Preflight: infra WP06 presente ─────────────────────────────
preflight_infra() {
  log "preflight: verificando infra estable de IoT (módulo iot-core / WP06)…"
  local missing=()
  aws_iot describe-thing-type --thing-type-name "$THING_TYPE_NAME"        >/dev/null 2>&1 || missing+=("thing-type $THING_TYPE_NAME")
  aws_iot get-policy            --policy-name      "$DEVICE_POLICY_NAME"   >/dev/null 2>&1 || missing+=("iot-policy $DEVICE_POLICY_NAME")
  aws_iot describe-thing-group  --thing-group-name "$SITE_GROUP"          >/dev/null 2>&1 || missing+=("thing-group $SITE_GROUP")
  aws_iot describe-thing-group  --thing-group-name "$CHANNEL_GROUP"       >/dev/null 2>&1 || missing+=("thing-group $CHANNEL_GROUP")
  aws_iot describe-role-alias   --role-alias       "$ROLE_ALIAS_NAME"     >/dev/null 2>&1 || warn "role-alias $ROLE_ALIAS_NAME ausente (se referencia en el .env; aplícalo en WP04/WP06)"
  if [ ${#missing[@]} -gt 0 ]; then
    printf '\n' >&2
    for m in "${missing[@]}"; do warn "falta recurso de infra: $m"; done
    die "infra IoT estable no aplicada. El runner MAD aplica el módulo iot-core (issue #42 / WP06) ANTES de provisionar devices. Aborta sin tocar nada."
  fi
  ok "preflight OK (thing-type, policy y grupos presentes)"
}

# ───────────────────────────── DynamoDB: item del registry ─────────────────────────────
# Escritor AUTORITATIVO de bootstrap: conditional put (attribute_not_exists(PK)). El hook
# `cam-counter-devices-register` (WP08) hace upsert TOLERANTE a item preexistente; no reescribe
# la identidad. PK=DEVICE#{device_id}, GSI1PK=CHANNEL#{release_channel}, GSI1SK=DEVICE#{device_id}.
register_device_item() {
  local cam_json
  cam_json="$(printf '%s\n' "${CAMERA_IDS[@]}" | jq -R . | jq -s '{L: map({S: .})}')"
  local item
  item="$(jq -n \
    --arg pk "DEVICE#${DEVICE_ID}" \
    --arg gsi1pk "CHANNEL#${CHANNEL}" \
    --arg gsi1sk "DEVICE#${DEVICE_ID}" \
    --arg device "$DEVICE_ID" \
    --arg site "$SITE_ID" \
    --arg channel "$CHANNEL" \
    --argjson cams "$cam_json" \
    --arg sv "$SCHEMA_VERSION" \
    '{
      PK:              {S: $pk},
      GSI1PK:          {S: $gsi1pk},
      GSI1SK:          {S: $gsi1sk},
      device_id:       {S: $device},
      site_id:         {S: $site},
      camera_ids:      $cams,
      release_channel: {S: $channel},
      status:          {S: "offline"},
      schema_version:  {N: $sv}
    }')"
  if aws dynamodb put-item \
        --table-name "$DEVICES_TABLE" \
        --item "$item" \
        --condition-expression "attribute_not_exists(PK)" \
        --region "$AWS_REGION" >/dev/null 2>&1; then
    ok "item registrado en $DEVICES_TABLE (PK=DEVICE#${DEVICE_ID}, channel=$CHANNEL)"
  else
    warn "item DEVICE#${DEVICE_ID} ya existía en $DEVICES_TABLE — no se sobrescribe (idempotente)"
  fi
}

# ───────────────────────────── Generación del .env del bundle ─────────────────────────────
# SOLO claves CAMCOUNTER_* (las que lee el código). Topics derivados del device_id (mismo canon
# que la policy). SIN AWS_ACCESS_KEY_ID/SECRET: los clips suben por el role-alias (WP04).
write_env() {
  local endpoint="$1"
  cat > "$ENV_PATH" <<EOF
# === cam-counter — .env de identidad IoT del device (generado por provision-device.sh) ===
# Generado para Thing '${THING_NAME}'. NO contiene credenciales AWS (los clips suben por el
# role-alias '${ROLE_ALIAS_NAME}', no por llaves estáticas). Distribuir SOLO por canal seguro.

# --- Identidad (slugs validados) ---
CAMCOUNTER_SITE_ID=${SITE_ID}
CAMCOUNTER_DEVICE_ID=${DEVICE_ID}
CAMCOUNTER_CAMERA_COUNT=${CAMERA_COUNT}
CAMCOUNTER_RELEASE_CHANNEL=${CHANNEL}

# --- Conexión MQTT a AWS IoT Core ---
CAMCOUNTER_AWS_REGION=${AWS_REGION}
CAMCOUNTER_IOT_ENDPOINT=${endpoint}
CAMCOUNTER_THING_NAME=${THING_NAME}
CAMCOUNTER_MQTT_CLIENT_ID=${THING_NAME}
CAMCOUNTER_ROLE_ALIAS=${ROLE_ALIAS_NAME}

# --- Rutas de identidad X.509 en el device (naming-standard §2) ---
CAMCOUNTER_IOT_CERT_PATH=/etc/cam-counter/certs/device.cert.pem
CAMCOUNTER_IOT_PRIVATE_KEY_PATH=/etc/cam-counter/certs/device.private.key
CAMCOUNTER_IOT_ROOT_CA_PATH=/etc/cam-counter/certs/AmazonRootCA1.pem

# --- Topics MQTT (derivados del device_id — mismo canon que la policy, naming-standard §3) ---
CAMCOUNTER_TOPIC_EVENTS=${TOPIC_BASE}/events/crossing
CAMCOUNTER_TOPIC_STATUS=${TOPIC_BASE}/status
CAMCOUNTER_TOPIC_TELEMETRY=${TOPIC_BASE}/telemetry
CAMCOUNTER_TOPIC_CMD=${TOPIC_BASE}/cmd

# --- Transporte de sincronización edge→cloud ---
# 'direct' = boto3 directo (comportamiento actual, no rompe el stack). 'iot' = MQTT (WP futuros).
CAMCOUNTER_SYNC_TRANSPORT=direct
EOF
  ok ".env generado (solo claves CAMCOUNTER_*, sin credenciales AWS): $ENV_PATH"
}

# ───────────────────────────── Empaquetado del bundle ─────────────────────────────
package_bundle() {
  local endpoint="$1"
  cat > "${OUT_DIR}/INSTALL.txt" <<EOF
cam-counter — device-bundle para ${THING_NAME}

Instalar en el Pi:
  sudo mkdir -p /etc/cam-counter/certs
  sudo cp certs/device.cert.pem certs/device.private.key certs/AmazonRootCA1.pem /etc/cam-counter/certs/
  sudo chmod 600 /etc/cam-counter/certs/device.private.key
  cp .env <ruta de despliegue del servicio>/.env
  # endpoint IoT ATS: ${endpoint}

La llave privada NUNCA salió de tu host de provisioning (solo se envió el CSR a AWS).
EOF
  ( cd "$OUT_DIR" && tar -czf "$BUNDLE_PATH" \
      certs/device.cert.pem certs/device.private.key certs/AmazonRootCA1.pem .env INSTALL.txt )
  ok "bundle empaquetado: $BUNDLE_PATH"
}

# ───────────────────────────── Descarga de la Root CA ─────────────────────────────
fetch_root_ca() {
  if [ -s "$ROOT_CA_PATH" ]; then log "AmazonRootCA1.pem ya presente (reuso)"; return; fi
  log "descargando AmazonRootCA1.pem"
  if command -v curl >/dev/null 2>&1; then
    curl -fsSL "$ROOT_CA_URL" -o "$ROOT_CA_PATH"
  else
    wget -qO "$ROOT_CA_PATH" "$ROOT_CA_URL"
  fi
  [ -s "$ROOT_CA_PATH" ] || die "no se pudo descargar la Root CA"
}

# ─────────────────────────────────────── Modos ───────────────────────────────────────
thing_principals() {
  aws_iot list-thing-principals --thing-name "$THING_NAME" 2>/dev/null | jq -r '.principals[]?' || true
}

create_thing_if_absent() {
  local attrs
  attrs="$(jq -nc --arg s "$SITE_ID" --arg d "$DEVICE_ID" --arg c "$CHANNEL" \
            '{attributes: {site_id: $s, device_id: $d, channel: $c}, merge: true}')"
  if aws_iot describe-thing --thing-name "$THING_NAME" >/dev/null 2>&1; then
    log "Thing '$THING_NAME' ya existe — actualizando atributos (idempotente)"
    aws_iot update-thing --thing-name "$THING_NAME" --thing-type-name "$THING_TYPE_NAME" \
      --attribute-payload "$attrs" >/dev/null
  else
    log "creando Thing '$THING_NAME' (thing-type $THING_TYPE_NAME)"
    aws_iot create-thing --thing-name "$THING_NAME" --thing-type-name "$THING_TYPE_NAME" \
      --attribute-payload "$(jq -nc --arg s "$SITE_ID" --arg d "$DEVICE_ID" --arg c "$CHANNEL" \
            '{attributes: {site_id: $s, device_id: $d, channel: $c}}')" >/dev/null
  fi
  ok "Thing '$THING_NAME' presente con atributos site_id/device_id/channel"
}

add_to_groups() {
  for g in "$SITE_GROUP" "$CHANNEL_GROUP"; do
    aws_iot add-thing-to-thing-group --thing-name "$THING_NAME" --thing-group-name "$g" >/dev/null
    ok "Thing en grupo '$g'"
  done
}

create_and_attach_cert() {
  log "creando certificado desde el CSR (--set-as-active)"
  local resp arn id
  resp="$(aws_iot create-certificate-from-csr --certificate-signing-request "file://${CSR_PATH}" --set-as-active)"
  arn="$(echo "$resp" | jq -r '.certificateArn')"
  id="$(echo "$resp"  | jq -r '.certificateId')"
  echo "$resp" | jq -r '.certificatePem' > "$CERT_PATH"
  chmod 600 "$CERT_PATH"
  [ -s "$CERT_PATH" ] || die "cert vacío tras create-certificate-from-csr"
  log "attach policy '$DEVICE_POLICY_NAME' al cert"
  aws_iot attach-policy --policy-name "$DEVICE_POLICY_NAME" --target "$arn" >/dev/null
  log "attach del cert al Thing (attach-thing-principal)"
  aws_iot attach-thing-principal --thing-name "$THING_NAME" --principal "$arn" >/dev/null
  echo "$id" > "$CERTID_PATH"
  echo "$arn"   # stdout: ARN para el resumen final
}

deactivate_old_certs() {
  # Desvincula y desactiva TODOS los certs adjuntos al Thing salvo $keep_id (el nuevo).
  local keep_id="${1:-}"
  while read -r principal; do
    [ -n "$principal" ] || continue
    local cid="${principal##*/}"
    [ "$cid" != "$keep_id" ] || continue
    log "retirando cert antiguo $cid (detach policy/principal + INACTIVE)"
    aws_iot detach-policy --policy-name "$DEVICE_POLICY_NAME" --target "$principal" >/dev/null 2>&1 || true
    aws_iot detach-thing-principal --thing-name "$THING_NAME" --principal "$principal" >/dev/null 2>&1 || true
    aws_iot update-certificate --certificate-id "$cid" --new-status INACTIVE >/dev/null 2>&1 || true
  done < <(thing_principals)
}

summary() {
  local arn="$1" endpoint="$2" checksum
  checksum="$(sha256sum "$BUNDLE_PATH" | awk '{print $1}')"
  printf '\n' >&2
  ok "PROVISIONING COMPLETO — Thing '$THING_NAME'"
  cat >&2 <<EOF

  certificateArn : ${arn}
  IoT endpoint   : ${endpoint}
  bundle         : ${BUNDLE_PATH}
  sha256(bundle) : ${checksum}

  RECORDATORIO: el bundle contiene la LLAVE PRIVADA del device. Distribúyelo SOLO por un
  canal seguro (p. ej. scp directo al Pi); NUNCA por git, email ni chat. Ningún artefacto
  de '${OUT_ROOT}' está versionado (.gitignore).
EOF
}

main() {
  mkdir -p "$OUT_DIR"
  log "modo=$MODE  thing=$THING_NAME  channel=$CHANNEL  cameras=${CAMERA_IDS[*]}  region=$AWS_REGION"

  if [ "$DRY_RUN" -eq 1 ]; then
    generate_key_and_csr
    write_env "$(iot_endpoint)"
    fetch_root_ca || warn "no se pudo descargar Root CA en dry-run (sin red); se omite"
    ok "DRY-RUN OK: validación de slugs + llave/CSR + .env locales generados (sin llamadas mutantes a AWS)"
    log "previsualización del .env:"; sed 's/^/    /' "$ENV_PATH" >&2
    exit 0
  fi

  local endpoint; endpoint="$(iot_endpoint)"

  case "$MODE" in
    provision)
      preflight_infra
      create_thing_if_absent
      add_to_groups
      local principals; principals="$(thing_principals)"
      local arn
      if [ -n "$principals" ]; then
        warn "el Thing ya tiene cert(s) adjunto(s) — no se crea uno nuevo (idempotente). Usa --rotate para reemplazar."
        arn="$(echo "$principals" | head -n1)"
        # Re-asegura policy/grupos/registry (idempotente) sin tocar el cert ni regenerar el bundle.
        aws_iot attach-policy --policy-name "$DEVICE_POLICY_NAME" --target "$arn" >/dev/null 2>&1 || true
        register_device_item
        ok "re-ejecución idempotente: identidad existente re-asegurada (cert intacto)"
        exit 0
      fi
      generate_key_and_csr
      arn="$(create_and_attach_cert)"
      register_device_item
      fetch_root_ca
      write_env "$endpoint"
      package_bundle "$endpoint"
      summary "$arn" "$endpoint"
      ;;
    rotate)
      aws_iot describe-thing --thing-name "$THING_NAME" >/dev/null 2>&1 || die "--rotate requiere un Thing existente: '$THING_NAME' no existe"
      preflight_infra
      generate_key_and_csr
      local arn; arn="$(create_and_attach_cert)"
      local new_id; new_id="$(cat "$CERTID_PATH")"
      deactivate_old_certs "$new_id"
      fetch_root_ca
      write_env "$endpoint"
      package_bundle "$endpoint"
      ok "ROTACIÓN completa: nuevo cert activo; certs anteriores desvinculados e INACTIVE"
      summary "$arn" "$endpoint"
      ;;
    revoke)
      aws_iot describe-thing --thing-name "$THING_NAME" >/dev/null 2>&1 || die "--revoke requiere un Thing existente: '$THING_NAME' no existe"
      local any=0
      while read -r principal; do
        [ -n "$principal" ] || continue
        any=1
        local cid="${principal##*/}"
        log "REVOCANDO cert $cid (detach policy/principal + REVOKED)"
        aws_iot detach-policy --policy-name "$DEVICE_POLICY_NAME" --target "$principal" >/dev/null 2>&1 || true
        aws_iot detach-thing-principal --thing-name "$THING_NAME" --principal "$principal" >/dev/null 2>&1 || true
        aws_iot update-certificate --certificate-id "$cid" --new-status REVOKED >/dev/null
      done < <(thing_principals)
      [ "$any" -eq 1 ] || warn "el Thing '$THING_NAME' no tenía certs adjuntos"
      # Marca el device offline en el registry (el item se conserva para auditoría).
      aws dynamodb update-item --table-name "$DEVICES_TABLE" \
        --key "$(jq -nc --arg pk "DEVICE#${DEVICE_ID}" '{PK: {S: $pk}}')" \
        --update-expression "SET #st = :st" \
        --expression-attribute-names '{"#st":"status"}' \
        --expression-attribute-values '{":st":{"S":"offline"}}' \
        --region "$AWS_REGION" >/dev/null 2>&1 || true
      ok "REVOCACIÓN completa: certs en REVOKED y desvinculados; device marcado offline"
      ;;
  esac
}

main
