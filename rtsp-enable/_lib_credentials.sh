#!/usr/bin/env bash
# Resolución ÚNICA y compartida de la credencial de la cámara (CAM_PASS).
#
# Cero secretos en git: este fichero NO contiene ninguna credencial. La contraseña
# real (= código de verificación de la pegatina, que es el password de `admin` para el
# SDK Hikvision y para el RTSP) se inyecta SIEMPRE desde fuera del repositorio.
#
# Prioridad de resolución (sin valor por defecto literal):
#   1) variable de entorno CAM_PASS (exportada)            -> recomendado
#   2) primera línea del fichero gitignored rtsp-enable/CAM_PASS (o $CAM_PASS_FILE)
#   3) si no hay ninguna: error claro a stderr + retorno no-cero (NUNCA un literal)
#
# El antiguo código de verificación que vivió en el historial de git está ROTADO/
# INVÁLIDO (el factory-reset es la única vía de recuperación del SDK), por lo que de
# todas formas existirá una credencial nueva que se aportará por env o por fichero.
#
# Uso típico desde otro script:
#   source "$(cd "$(dirname "$0")" && pwd)/_lib_credentials.sh"
#   resolve_cam_pass || exit 1
#   # ... ya puedes usar "$CAM_PASS"

# resolve_cam_pass: deja la credencial en la variable global CAM_PASS.
#   - return 0 si la credencial quedó resuelta (CAM_PASS no vacío).
#   - return 1 (con mensaje a stderr) si no hay credencial disponible.
resolve_cam_pass() {
  local _lib_dir
  _lib_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
  local _cred_file="${CAM_PASS_FILE:-$_lib_dir/CAM_PASS}"

  # 1) Entorno: si ya viene CAM_PASS exportada y no vacía, se usa tal cual.
  if [ -n "${CAM_PASS:-}" ]; then
    return 0
  fi

  # 2) Fichero gitignored: primera línea, sin CR/LF.
  if [ -f "$_cred_file" ]; then
    CAM_PASS="$(head -n1 "$_cred_file" | tr -d '\r\n')"
  fi

  # 3) Sin credencial -> fallo explícito (NO se cae a un literal por defecto).
  if [ -z "${CAM_PASS:-}" ]; then
    echo "ERROR: credencial de la cámara (CAM_PASS) no disponible." >&2
    echo "       Apórtala por entorno:   export CAM_PASS='...'        (recomendado)" >&2
    echo "       o por fichero local:     echo '...' > $_cred_file     (gitignored)" >&2
    echo "       El antiguo código de verificación del historial está ROTADO/INVÁLIDO." >&2
    return 1
  fi

  return 0
}
