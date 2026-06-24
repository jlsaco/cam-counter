#!/usr/bin/env bash
# run-probe.sh — construye y ejecuta el probe del SPIKE WP09 (issue #45) en la Pi5.
#
# Demuestra el contrato de runtime SEGURO para el Hailo en contenedor:
#   - `--device /dev/hailo0:/dev/hailo0`  (inyecta SOLO el nodo, no /dev entero)
#   - `--group-add <gid>`                 (GID del grupo dueño de /dev/hailo0)
#   - SIN `--privileged`                  (minimo privilegio)
#
# El GID se AUTODETECTA del nodo real del host: si el dispositivo es root:hailo
# 0660, el contenedor necesita ese GID; si es 0666 (world-rw) el group_add es
# inocuo pero se pasa igual para que el patron sea portable entre hosts.
#
# Uso:
#   docker/run-probe.sh                 # build + probe con frame sintetico
#   docker/run-probe.sh /tmp/people.jpg # build + probe con imagen real (personas)
#   SKIP_BUILD=1 docker/run-probe.sh    # solo run (imagen ya construida)
set -euo pipefail

IMAGE_TAG="${IMAGE_TAG:-cam-counter-edge:poc}"
HEF_DIR="${HEF_DIR:-/usr/share/hailo-models}"
DEV="/dev/hailo0"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# --- pre-flight: hardware real, ARM64, nodo presente -------------------------
arch="$(dpkg --print-architecture 2>/dev/null || uname -m)"
if [ "$arch" != "arm64" ] && [ "$arch" != "aarch64" ]; then
    echo "ERROR: este probe DEBE correr en ARM64 real (Pi5). Detectado: $arch." >&2
    exit 1
fi
if [ ! -e "$DEV" ]; then
    echo "ERROR: $DEV no existe. ¿Driver hailo_pci cargado? (modprobe hailo_pci)" >&2
    exit 1
fi

# GID dueño del nodo (numerico, portable: no asume que exista el grupo 'hailo').
DEV_GID="$(stat -c '%g' "$DEV")"
DEV_PERMS="$(stat -c '%a' "$DEV")"
echo ">> $DEV  gid=$DEV_GID perms=$DEV_PERMS"

# --- build (en la Pi5, ARM64 real) -------------------------------------------
if [ "${SKIP_BUILD:-0}" != "1" ]; then
    echo ">> stage HailoRT debs del host (bits identicos => match con driver)"
    "$SCRIPT_DIR/stage-debs.sh"
    echo ">> build $IMAGE_TAG (ARM64 real)"
    # Contexto = raiz del repo (WP17): la imagen edge necesita v1/edge ademas de
    # los .deb staged. El Dockerfile referencia docker/... y v1/... desde la raiz.
    docker build -f "$SCRIPT_DIR/edge.Dockerfile" -t "$IMAGE_TAG" "$SCRIPT_DIR/.."
fi

# --- run: minimo privilegio, solo el nodo Hailo, HEFs del host read-only -----
PROBE_IMAGE_ARG=()
MOUNT_IMG_ARG=()
if [ "${1:-}" != "" ]; then
    host_img="$1"
    [ -f "$host_img" ] || { echo "ERROR: imagen no existe: $host_img" >&2; exit 1; }
    MOUNT_IMG_ARG=(-v "$host_img:/tmp/probe_input.jpg:ro")
    PROBE_IMAGE_ARG=(-e "CAMCOUNTER_PROBE_IMAGE=/tmp/probe_input.jpg")
fi

echo ">> run probe (sin --privileged)"
# El ENTRYPOINT de la imagen de PRODUCCION (WP17) es el supervisor de conteo; aqui
# lo SOBREESCRIBIMOS para ejecutar el probe del spike (diagnostico del acceso Hailo).
exec docker run --rm \
    --device "$DEV:$DEV" \
    --group-add "$DEV_GID" \
    -v "$HEF_DIR:/usr/share/hailo-models:ro" \
    "${MOUNT_IMG_ARG[@]}" \
    "${PROBE_IMAGE_ARG[@]}" \
    --entrypoint python3 \
    "$IMAGE_TAG" /opt/cam-counter/hailo_probe.py
