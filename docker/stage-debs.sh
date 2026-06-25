#!/usr/bin/env bash
# stage-debs.sh — coloca los .deb de HailoRT del HOST en el contexto de build
# (docker/debs/) para que edge.Dockerfile instale EXACTAMENTE los mismos bits
# que corren en el host (garantia dura de HailoRT(contenedor) == driver(host)).
#
# Por que NO se instala desde el repo apt de Raspberry Pi dentro del Dockerfile:
# en Debian trixie, `sqv` (Sequoia) RECHAZA la firma SHA1 de la clave del repo
# `archive.raspberrypi.com` (SHA1 inseguro desde 2026-02-01). Instalar desde el
# .deb local del host evita ese problema de firma Y es mas reproducible: la
# imagen lleva el binario IDENTICO al que ya valido el host. Ver
# docs/poc-hailo-docker.md (seccion "Riesgo: clave SHA1 del repo RPi").
#
# Los .deb NO se commitean (ver .gitignore: docker/debs/). Este script los
# materializa en cada build desde la cache de apt del host (o los descarga).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DEST="$SCRIPT_DIR/debs"
CACHE="/var/cache/apt/archives"
HAILORT_VERSION="${HAILORT_VERSION:-4.23.0}"

mkdir -p "$DEST"
rm -f "$DEST"/*.deb

stage_one() {
    local glob="$1" name="$2"
    local src
    src="$(ls -1 $CACHE/$glob 2>/dev/null | head -n1 || true)"
    if [ -n "$src" ] && [ -r "$src" ]; then
        cp -f "$src" "$DEST/"
        echo ">> staged $name desde cache: $(basename "$src")"
        return 0
    fi
    # Fallback: descargar a $DEST con el paquete instalado en el host.
    echo ">> $name no esta en cache; intentando apt-get download..."
    ( cd "$DEST" && apt-get download "$name" ) && return 0
    echo "ERROR: no pude obtener $name (.deb). ¿Esta instalado en el host?" >&2
    return 1
}

stage_one "hailort_${HAILORT_VERSION}_*.deb" "hailort"
stage_one "python3-hailort_${HAILORT_VERSION}*.deb" "python3-hailort"

echo ">> debs en contexto de build:"
ls -la "$DEST"/*.deb
