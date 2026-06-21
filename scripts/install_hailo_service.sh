#!/usr/bin/env bash
# Instalador idempotente de la unit systemd `hailo-personas`.
#
# La unit versionada (v1/systemd/hailo-personas.service) NO contiene rutas
# absolutas: usa el placeholder __CAM_COUNTER_REPO__. Este instalador resuelve la
# ruta REAL del clon del repo y renderiza la unit sustituyendo el placeholder,
# de modo que la Pi puede clonar en CUALQUIER ruta y obtener una unit funcional
# sin editar a mano ni `sed` manuales.
#
# Idempotente: volver a ejecutarlo deja exactamente el mismo resultado (no
# duplica ni acumula nada). Si la unit destino ya coincide con el render, no
# reescribe.
#
# Funciona en x86 sin systemd (CI/dev): si no hay `systemctl`, sólo renderiza la
# unit (a un fichero temporal o al destino indicado) e imprime el resultado, sin
# fallar. Pasa `bash -n`.
#
# Variables de entorno opcionales:
#   CAM_COUNTER_REPO   Ruta del repo a inyectar (por defecto se autodetecta).
#   UNIT_DEST          Ruta destino de la unit renderizada
#                      (por defecto /etc/systemd/system/hailo-personas.service;
#                       útil para testear el render sin permisos de root).
#   NO_SYSTEMCTL=1     Fuerza el modo "sólo render" aunque exista systemctl.
set -euo pipefail

SERVICE_NAME="hailo-personas"
PLACEHOLDER="__CAM_COUNTER_REPO__"

# --- 1) Resolver la ruta real del repo (raíz del clon) de forma robusta --------
script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"   # .../<repo>/scripts
if [ -n "${CAM_COUNTER_REPO:-}" ]; then
  repo_root="$CAM_COUNTER_REPO"
elif repo_root="$(git -C "$script_dir" rev-parse --show-toplevel 2>/dev/null)"; then
  : # raíz vía git (camino normal en un clon)
else
  repo_root="$(cd "$script_dir/.." && pwd)"                  # fallback: scripts/..
fi

template="$repo_root/v1/systemd/${SERVICE_NAME}.service"
if [ ! -f "$template" ]; then
  echo "ERROR: no se encuentra la plantilla de la unit: $template" >&2
  exit 1
fi

# --- 2) Renderizar la unit sustituyendo el placeholder por la ruta real --------
# Sustitución segura sin `sed` (la ruta puede contener caracteres especiales).
rendered="$(REPO="$repo_root" PH="$PLACEHOLDER" python3 - "$template" <<'PY'
import os, sys
tpl = open(sys.argv[1], encoding="utf-8").read()
sys.stdout.write(tpl.replace(os.environ["PH"], os.environ["REPO"]))
PY
)"

# --- 3) Decidir destino y modo (con o sin systemd) -----------------------------
unit_dest="${UNIT_DEST:-/etc/systemd/system/${SERVICE_NAME}.service}"

have_systemctl=1
{ [ -n "${NO_SYSTEMCTL:-}" ] || ! command -v systemctl >/dev/null 2>&1; } && have_systemctl=0

if [ "$have_systemctl" -eq 0 ]; then
  # Modo sólo-render (x86 / CI / dry-run): si UNIT_DEST es escribible, lo escribe;
  # si no, usa un temporal. Siempre imprime el resultado. No falla.
  dest="$unit_dest"
  dest_dir="$(dirname "$dest")"
  if [ ! -d "$dest_dir" ] || [ ! -w "$dest_dir" ]; then
    dest="$(mktemp -t "${SERVICE_NAME}.service.XXXXXX")"
  fi
  printf '%s' "$rendered" > "$dest"
  echo "[install_hailo_service] systemd no disponible; unit renderizada en: $dest" >&2
  echo "[install_hailo_service] repo detectado: $repo_root" >&2
  printf '%s' "$rendered"
  exit 0
fi

# --- 4) Instalación real con systemd (idempotente) -----------------------------
# Escribe sólo si cambia (idempotencia: no toca la unit si ya coincide).
if [ -f "$unit_dest" ] && printf '%s' "$rendered" | cmp -s - "$unit_dest"; then
  echo "[install_hailo_service] unit ya actualizada en $unit_dest (sin cambios)" >&2
else
  printf '%s' "$rendered" > "$unit_dest"
  echo "[install_hailo_service] unit escrita en $unit_dest" >&2
fi

systemctl daemon-reload
systemctl enable --now "$SERVICE_NAME"
echo "[install_hailo_service] $SERVICE_NAME habilitado e iniciado (repo: $repo_root)" >&2
