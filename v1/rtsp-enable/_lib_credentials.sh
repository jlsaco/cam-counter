#!/usr/bin/env bash
# Resolución centralizada de la credencial de la cámara (CAM_PASS).
# CERO secretos en git: la contraseña (código de verificación del usuario `admin`,
# usada tanto por el SDK Hikvision como por el stream RTSP) se inyecta SIEMPRE desde
# fuera del repositorio, nunca como literal versionado.
#
# Orden de resolución (el primero que exista, gana):
#   1) Variable de entorno  $CAM_PASS  (si está exportada y no vacía).
#   2) Fichero gitignored    rtsp-enable/CAM_PASS  (primera línea; junto a esta librería).
#   3) Si no hay ninguna  ->  error claro a stderr + retorno no-cero (SIN default literal).
#
# Uso típico desde otro script:
#   source "$(dirname "$0")/_lib_credentials.sh"      # (o ../_lib_credentials.sh)
#   CAM_PASS="$(resolve_cam_pass)" || exit 1
#
# NOTA: el código de verificación que aparece en el historial de git está ROTADO/INVÁLIDO;
# aporta la credencial real vía  export CAM_PASS=...  o el fichero gitignored rtsp-enable/CAM_PASS.

resolve_cam_pass() {
  # 1) Entorno
  if [ -n "${CAM_PASS:-}" ]; then
    printf '%s' "$CAM_PASS"
    return 0
  fi

  # 2) Fichero gitignored, ubicado junto a esta librería (rtsp-enable/CAM_PASS)
  local lib_dir pass_file pass
  lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  pass_file="$lib_dir/CAM_PASS"
  if [ -f "$pass_file" ]; then
    pass="$(head -n1 "$pass_file")"
    if [ -n "$pass" ]; then
      printf '%s' "$pass"
      return 0
    fi
  fi

  # 3) Sin credencial -> fallo explícito (nunca caer a un literal por defecto)
  {
    echo "ERROR: credencial de la cámara (CAM_PASS) no disponible."
    echo "       Apórtala por entorno:        export CAM_PASS=<codigo_de_verificacion>"
    echo "       o crea el fichero gitignored: $pass_file"
    echo "       (el código que aparece en el historial de git está ROTADO/INVÁLIDO)."
  } >&2
  return 1
}
