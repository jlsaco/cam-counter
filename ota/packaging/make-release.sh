#!/usr/bin/env bash
# make-release.sh — empaqueta el borde (v1/) en un tarball OTA arm64 DETERMINISTA.
#
# Produce `dist/cam-counter-edge-<version>-arm64.tar.gz` con:
#   - el producto del borde (v1/edge + v1/api + v1/ui/dist + systemd + scripts/version.py +
#     contracts/), SIN rtsp-enable (utilidad host box64/Java, INTOCABLE y fuera del payload),
#     SIN docs/detection (contienen rutas /home/pi), SIN caches/node_modules/.git/estado;
#   - un `bundle-manifest.json` (valida contra contracts/bundle_manifest.schema.json) que
#     fija version, git_sha, entrypoint y el `native_blob` por {key, sha256} (el blob nativo
#     NO va embebido: vive en S3, se referencia por key+sha256).
#
# DETERMINISTA: orden de archivos estable (--sort=name), mtime fijo (SOURCE_DATE_EPOCH del
# commit HEAD), uid/gid 0 numéricos, gzip -n (sin timestamp). Mismo input -> mismo sha256.
#
# CERO SECRETOS: el empaquetador ABORTA si algún archivo a incluir contiene el secreto de
# cámara quemado (RWCHBY) o una ruta absoluta /home/pi (rutas absolutas rompen la portabilidad
# y no deben viajar en el artefacto).
#
# Flags:
#   --dry-run            imprime qué haría (incluida la versión) sin construir.
#   --list-bundle-files  lista (rutas repo-relativas) los archivos que entrarían, sin construir.
#   --out-dir DIR        directorio de salida (default: dist).
#
# Pasa `bash -n` y `shellcheck`.
set -euo pipefail

# ── localización del repo (sin rutas absolutas fijas) ────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/../.." && pwd)"
cd "$REPO_ROOT"

OUT_DIR="dist"
MODE="build"

while [ "$#" -gt 0 ]; do
  case "$1" in
    --dry-run) MODE="dry-run" ;;
    --list-bundle-files) MODE="list" ;;
    --out-dir) shift; OUT_DIR="${1:?--out-dir requiere un valor}" ;;
    -h|--help) sed -n '2,30p' "$0"; exit 0 ;;
    *) echo "flag desconocido: $1" >&2; exit 2 ;;
  esac
  shift
done

# ── versión (única fuente: scripts/version.py vía tags git) ──────────────────
VERSION="$(python3 scripts/version.py)"
GIT_SHA="$(python3 scripts/version.py --json | python3 -c 'import json,sys; print(json.load(sys.stdin)["git_sha"])')"
ARTIFACT_NAME="cam-counter-edge-${VERSION}-arm64.tar.gz"

# Entrypoint del producto dentro del bundle (relativo a la raíz del bundle).
ENTRYPOINT="edge/run_edge.sh"
MIN_AGENT_VERSION="${MIN_AGENT_VERSION:-0.1.0}"
PYTHON_TARGET="${PYTHON_TARGET:-3.11}"

# native_blob: NO se embebe. Se referencia por key+sha256 (overridable por el release CI con
# el blob nativo real). Default: placeholder claramente marcado (sha de 64 ceros).
NATIVE_BLOB_KEY="${NATIVE_BLOB_KEY:-native/box64-sysroot-arm64.tar.gz}"
NATIVE_BLOB_SHA256="${NATIVE_BLOB_SHA256:-0000000000000000000000000000000000000000000000000000000000000000}"

# SOURCE_DATE_EPOCH: timestamp del commit HEAD (reproducible por commit), fallback fijo.
if [ -z "${SOURCE_DATE_EPOCH:-}" ]; then
  SOURCE_DATE_EPOCH="$(git -C "$REPO_ROOT" log -1 --format=%ct 2>/dev/null || echo 1700000000)"
fi

# ── conjunto de archivos a incluir (find con prune de exclusiones) ───────────
# Incluye v1/{edge,api,ui/dist,systemd} + scripts/version.py + contracts/*.json.
# Excluye: rtsp-enable, docs, detection, tests, caches, node_modules, dist, egg-info, .git,
# estado (*.db, *.sqlite*), venvs.
collect_files() {
  {
    find v1/edge v1/api v1/systemd \
      -type d \( -name '__pycache__' -o -name '.mypy_cache' -o -name '.pytest_cache' \
                 -o -name '.ruff_cache' -o -name 'node_modules' -o -name '.venv' \
                 -o -name 'tests' -o -name 'e2e' -o -name '*.egg-info' \) -prune -o \
      -type f ! -name '*.pyc' ! -name '*.db' ! -name '*.db-shm' ! -name '*.db-wal' \
               ! -name '*.db-journal' ! -name '*.sqlite' ! -name '*.sqlite3' \
               ! -name '*.log' -print 2>/dev/null
    # SPA ya construida (si existe el build de Vite).
    if [ -d v1/ui/dist ]; then
      find v1/ui/dist -type f -print 2>/dev/null
    fi
    # Versionado + contratos compartidos (para self-report offline y validación).
    echo scripts/version.py
    find contracts -maxdepth 1 -type f -name '*.json' -print 2>/dev/null
  } | LC_ALL=C sort -u
}

# ── guard CERO SECRETOS / rutas absolutas ────────────────────────────────────
assert_no_secrets() {
  local files="$1" hits
  # Secreto de cámara quemado.
  hits="$(printf '%s\n' "$files" | xargs -r grep -rliE 'RWCHBY' 2>/dev/null || true)"
  if [ -n "$hits" ]; then
    echo "ABORT: el secreto de cámara (RWCHBY) aparece en archivos a empaquetar:" >&2
    echo "$hits" >&2
    exit 1
  fi
  # Rutas absolutas /home/pi (rompen portabilidad).
  hits="$(printf '%s\n' "$files" | xargs -r grep -rliE '/home/pi' 2>/dev/null || true)"
  if [ -n "$hits" ]; then
    echo "ABORT: ruta absoluta /home/pi en archivos a empaquetar (no portable):" >&2
    echo "$hits" >&2
    exit 1
  fi
}

FILES="$(collect_files)"

case "$MODE" in
  list)
    printf '%s\n' "$FILES"
    exit 0
    ;;
  dry-run)
    echo "make-release (dry-run)"
    echo "version: ${VERSION}"
    echo "git_sha: ${GIT_SHA}"
    echo "artifact: ${OUT_DIR}/${ARTIFACT_NAME}"
    echo "entrypoint: ${ENTRYPOINT}"
    echo "native_blob: ${NATIVE_BLOB_KEY} (sha256=${NATIVE_BLOB_SHA256})"
    echo "files: $(printf '%s\n' "$FILES" | grep -c .)"
    echo "source_date_epoch: ${SOURCE_DATE_EPOCH}"
    assert_no_secrets "$FILES"
    echo "secret-scan: OK (sin RWCHBY ni /home/pi)"
    exit 0
    ;;
esac

# ── build determinista ───────────────────────────────────────────────────────
assert_no_secrets "$FILES"

STAGE="$(mktemp -d)"
trap 'rm -rf "$STAGE"' EXIT
BUNDLE_ROOT="$STAGE/cam-counter-edge-${VERSION}"
mkdir -p "$BUNDLE_ROOT"

# Copia con el prefijo v1/ APLANADO (v1/edge -> edge), conservando el resto de rutas.
while IFS= read -r f; do
  [ -n "$f" ] || continue
  rel="${f#v1/}"            # quita el prefijo v1/ si lo tiene
  dest="$BUNDLE_ROOT/$rel"
  mkdir -p "$(dirname "$dest")"
  cp -p "$f" "$dest"
done <<< "$FILES"

# bundle-manifest.json (valida contra contracts/bundle_manifest.schema.json).
BUILT_AT="$(date -u -d "@${SOURCE_DATE_EPOCH}" +%Y-%m-%dT%H:%M:%SZ 2>/dev/null \
            || date -u +%Y-%m-%dT%H:%M:%SZ)"
cat > "$BUNDLE_ROOT/bundle-manifest.json" <<JSON
{
  "schema_version": 1,
  "version": "${VERSION}",
  "git_sha": "${GIT_SHA}",
  "built_at": "${BUILT_AT}",
  "min_agent_version": "${MIN_AGENT_VERSION}",
  "entrypoint": "${ENTRYPOINT}",
  "python": "${PYTHON_TARGET}",
  "native_blob": {
    "key": "${NATIVE_BLOB_KEY}",
    "sha256": "${NATIVE_BLOB_SHA256}"
  }
}
JSON

mkdir -p "$OUT_DIR"
OUT_PATH="${OUT_DIR}/${ARTIFACT_NAME}"

# Tar DETERMINISTA: orden estable, mtime fijo, uid/gid 0 numéricos, gzip -n (sin timestamp).
tar --sort=name \
    --mtime="@${SOURCE_DATE_EPOCH}" \
    --owner=0 --group=0 --numeric-owner \
    --format=gnu \
    -C "$STAGE" \
    -cf - "cam-counter-edge-${VERSION}" \
  | gzip -n -9 > "$OUT_PATH"

SHA256="$(sha256sum "$OUT_PATH" | awk '{print $1}')"
echo "$SHA256  $ARTIFACT_NAME" > "${OUT_PATH}.sha256"

echo "built: ${OUT_PATH}"
echo "version: ${VERSION}"
echo "sha256: ${SHA256}"
echo "size_bytes: $(wc -c < "$OUT_PATH" | tr -d ' ')"
