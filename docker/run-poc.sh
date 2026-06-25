#!/usr/bin/env bash
# run-poc.sh — build + run del PoC Hailo-en-Docker (WP09 / IOT-45) EN LA Pi5.
#
# Construye docker/edge.Dockerfile en ARM64 REAL (no qemu) y corre la sonda
# docker/probe_hailo.py dentro del contenedor mapeando /dev/hailo0 SIN --privileged.
# Emite el veredicto GO/NO-GO por código de salida (0 = GO, !=0 = NO-GO).
#
# GUARDARRAÍL: este spike NO toca Terraform ni la identidad admin `raspberry`/~/.aws.
# NO commitea blobs: el keyring público y el HEF se stagean en el contexto de build
# y están gitignored (ver .gitignore en docker/).
#
# Uso:
#   docker/run-poc.sh            # build + run, imprime GO/NO-GO
#   NO_BUILD=1 docker/run-poc.sh # sólo run (reusa la imagen ya construida)
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
IMAGE="cam-counter-edge-poc:hailo-4.23.0"
HAILO_DEV="/dev/hailo0"
MODELS_DIR="/usr/share/hailo-models"
KEYRING_SRC="/usr/share/keyrings/raspberrypi-archive-keyring.pgp"

# --- 0) Pre-vuelo: hardware real, no x86/qemu --------------------------------
arch="$(uname -m)"
if [ "$arch" != "aarch64" ] && [ "$arch" != "arm64" ]; then
  echo "ABORTA :: este PoC SÓLO es válido en ARM64 real (Pi5); arch=$arch (x86/qemu NO cuenta)." >&2
  exit 2
fi
if [ ! -e "$HAILO_DEV" ]; then
  echo "ABORTA :: no existe $HAILO_DEV en el host (¿driver hailo_pci cargado?)." >&2
  exit 2
fi
page="$(getconf PAGE_SIZE)"
echo "[run-poc] host: arch=$arch page_size=${page} (esperado 16384 en Pi5)"
echo "[run-poc] firmware del acelerador:"
hailortcli fw-control identify 2>/dev/null | grep -E 'Firmware Version|Board Name|Device Architecture' | sed 's/^/    /' || true

# GID y modo del device: decide si --group-add es estrictamente necesario.
dev_gid="$(stat -c '%g' "$HAILO_DEV")"
dev_mode="$(stat -c '%a' "$HAILO_DEV")"
echo "[run-poc] $HAILO_DEV gid=$dev_gid mode=$dev_mode"

# --- 1) Stage del contexto de build (blobs gitignored) -----------------------
if [ ! -f "$KEYRING_SRC" ]; then
  echo "ABORTA :: keyring de Raspberry Pi ausente ($KEYRING_SRC)." >&2
  exit 2
fi
cp -f "$KEYRING_SRC" "$HERE/raspberrypi-archive-keyring.pgp"
trap 'rm -f "$HERE/raspberrypi-archive-keyring.pgp"' EXIT

# --- 2) Build EN LA Pi (ARM64 real) ------------------------------------------
if [ -z "${NO_BUILD:-}" ]; then
  echo "[run-poc] construyendo $IMAGE (ARM64 real)…"
  docker build -f "$HERE/edge.Dockerfile" -t "$IMAGE" "$HERE"
fi

# --- 3) Run: device mapeado, --group-add, SIN privileged ---------------------
# group_add del GID del device: necesario si la udev rule da 0660 root:hailo.
# En este host el device es 0666 (world-rw) → la apertura funciona incluso sin él,
# pero lo pasamos igualmente para que el PoC sea válido en hosts con udev estricta.
echo "[run-poc] corriendo sonda (device=$HAILO_DEV, group-add=$dev_gid, SIN --privileged)…"
set +e
docker run --rm \
  --device "${HAILO_DEV}:${HAILO_DEV}" \
  --group-add "$dev_gid" \
  -v "${MODELS_DIR}:${MODELS_DIR}:ro" \
  "$IMAGE"
rc=$?
set -e

echo "----------------------------------------------------------------------"
if [ "$rc" -eq 0 ]; then
  echo "[run-poc] VEREDICTO: GO ✅ (rc=0)"
else
  echo "[run-poc] VEREDICTO: NO-GO ❌ (rc=$rc)"
fi
exit "$rc"
