#!/usr/bin/env bash
# build_edge_artifact.sh — wrapper de build invocado por CI (release.yml).
#
# Delega en `ota/packaging/make-release.sh` (empaquetado determinista del borde desde v1/) y
# deja el artefacto + su `.sha256` en el directorio de salida. Separa el "cómo se construye"
# (make-release.sh) del "cuándo lo invoca CI" (este wrapper), para que el workflow no dependa
# de los detalles del empaquetador.
#
# Uso:
#   scripts/build_edge_artifact.sh [OUT_DIR]      # default OUT_DIR=dist
#
# Variables de entorno reconocidas (se pasan a make-release.sh):
#   NATIVE_BLOB_KEY, NATIVE_BLOB_SHA256  — referencia del blob nativo (no embebido).
#   MIN_AGENT_VERSION, PYTHON_TARGET, SOURCE_DATE_EPOCH.
#
# Pasa `bash -n` y `shellcheck`.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"
cd "$REPO_ROOT"

OUT_DIR="${1:-dist}"

bash ota/packaging/make-release.sh --out-dir "$OUT_DIR"

# Exporta variables útiles para los pasos siguientes de CI (firma/upload/manifiesto) cuando
# se ejecuta dentro de GitHub Actions ($GITHUB_ENV disponible).
VERSION="$(python3 scripts/version.py)"
ARTIFACT="${OUT_DIR}/cam-counter-edge-${VERSION}-arm64.tar.gz"
if [ -n "${GITHUB_ENV:-}" ] && [ -f "$ARTIFACT" ]; then
  {
    echo "EDGE_ARTIFACT_PATH=${ARTIFACT}"
    echo "EDGE_ARTIFACT_VERSION=${VERSION}"
    echo "EDGE_ARTIFACT_SHA256=$(sha256sum "$ARTIFACT" | awk '{print $1}')"
  } >> "$GITHUB_ENV"
fi
