#!/usr/bin/env bash
# Instalador idempotente de la unit systemd del servicio de detección edge (hailo-personas).
#
# La unit versionada (v1/systemd/hailo-personas.service) NO contiene rutas absolutas: usa
# el placeholder __CAM_COUNTER_REPO__ que este script sustituye por la ruta REAL del clon.
# Así la Pi puede clonar el repo en cualquier ruta y obtener una unit funcional, sin el
# antiguo `sed` manual sobre /home/pi/Documents/hailo-ezviz-personas.
#
# Idempotente: volver a ejecutarlo deja exactamente el mismo resultado (renderiza y
# escribe el mismo contenido; no duplica nada).
#
# Uso:
#   sudo scripts/install_hailo_service.sh            # render + instala en /etc/systemd/system + enable --now
#   DEST=/tmp/unit.service scripts/install_hailo_service.sh   # render a un destino alternativo (sin root)
#
# Variables de entorno opcionales:
#   CAM_COUNTER_REPO   Fuerza la ruta del repo a usar como sustitución del placeholder.
#                      Por defecto se deriva con `git rev-parse --show-toplevel` (o por
#                      dirname del propio script como fallback).
#   DEST               Ruta de destino de la unit renderizada. Por defecto
#                      /etc/systemd/system/hailo-personas.service. Útil para testear el
#                      render sin permisos de root ni systemd.

set -euo pipefail

PLACEHOLDER='__CAM_COUNTER_REPO__'
UNIT_NAME='hailo-personas.service'

# --- 1) Resolver la ruta REAL del repo (raíz del clon) de forma robusta --------------
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ -n "${CAM_COUNTER_REPO:-}" ]; then
  repo_root="$CAM_COUNTER_REPO"
elif repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)"; then
  : # repo_root resuelto por git
else
  # Fallback sin git: scripts/ cuelga de la raíz del repo -> dirname del dir del script.
  repo_root="$(cd "$script_dir/.." && pwd)"
fi

src_unit="$repo_root/v1/systemd/$UNIT_NAME"
if [ ! -f "$src_unit" ]; then
  echo "ERROR: no encuentro la unit fuente en $src_unit" >&2
  exit 1
fi

# --- 2) Renderizar la unit sustituyendo el placeholder por la ruta real --------------
rendered="$(sed "s|$PLACEHOLDER|$repo_root|g" "$src_unit")"

# Comprobación: no debe quedar ningún placeholder sin sustituir.
if printf '%s\n' "$rendered" | grep -q "$PLACEHOLDER"; then
  echo "ERROR: quedó el placeholder $PLACEHOLDER sin sustituir en la unit renderizada." >&2
  exit 1
fi

# --- 3) Determinar destino y si hay systemd disponible -------------------------------
default_dest='/etc/systemd/system/'"$UNIT_NAME"
dest="${DEST:-$default_dest}"

have_systemd=0
if command -v systemctl >/dev/null 2>&1 && [ -d /run/systemd/system ] && [ -z "${DEST:-}" ]; then
  have_systemd=1
fi

if [ "$have_systemd" -eq 0 ]; then
  # x86 / CI / sin systemd (o destino alternativo): solo renderizamos. Idempotente.
  if [ -n "${DEST:-}" ]; then
    printf '%s\n' "$rendered" > "$dest"
    echo "Unit renderizada (sin instalar systemd) en: $dest"
  else
    echo "# systemd no disponible: unit renderizada a stdout (dry-run, no se instala)." >&2
    printf '%s\n' "$rendered"
  fi
  exit 0
fi

# --- 4) Instalación real con systemd (idempotente) -----------------------------------
# Escribir sólo si el contenido difiere (mantiene el resultado idéntico al re-ejecutar).
if [ -f "$dest" ] && printf '%s\n' "$rendered" | cmp -s - "$dest"; then
  echo "Unit ya actualizada en $dest (sin cambios)."
else
  printf '%s\n' "$rendered" > "$dest"
  echo "Unit instalada/actualizada en $dest"
fi

systemctl daemon-reload
systemctl enable --now "$UNIT_NAME"
echo "Servicio $UNIT_NAME habilitado y arrancado."
