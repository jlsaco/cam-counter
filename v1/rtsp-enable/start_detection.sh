#!/usr/bin/env bash
# Arranque robusto: resuelve la camara por MAC, garantiza RTSP activo, lanza deteccion.
# Las rutas se derivan de la ubicacion de este script (funciona desde cualquier carpeta).
BASE="$(cd "$(dirname "$0")" && pwd)"          # .../rtsp-enable
REPO="$(dirname "$BASE")"                       # raiz del repo
DETECTION="$REPO/detection/yolo_personas_mt.py"
MAC="ac:1c:26"

# 0) Resolver credencial de la camara (entorno $CAM_PASS o fichero gitignored
#    rtsp-enable/CAM_PASS). Falla pronto -antes del escaneo de red- si no esta;
#    nunca se usa un literal por defecto.
source "$BASE/_lib_credentials.sh"
CAM_PASS="$(resolve_cam_pass)" || exit 1

# 1) Resolver IP de la camara
for i in $(seq 1 254); do ping -c1 -W1 192.168.1.$i >/dev/null 2>&1 & done; wait
CAM=$(ip neigh | grep -i "$MAC" | grep -oE '192\.168\.1\.[0-9]+' | head -1)
[ -z "$CAM" ] && { echo "camara no encontrada, reintentar"; sleep 10; exit 1; }
echo "Camara en $CAM"; echo "$CAM" > "$BASE/CAM_IP"

# 2) Garantizar RTSP activo
if ! timeout 3 bash -c "echo >/dev/tcp/$CAM/554" 2>/dev/null; then
  echo "RTSP cerrado, activando..."
  bash "$BASE/rtsp_enable_final.sh"
fi

# 3) Lanzar deteccion (multi-hilo)
export OPENCV_FFMPEG_CAPTURE_OPTIONS="rtsp_transport;tcp"
exec /usr/bin/python3 -u "$DETECTION" "rtsp://admin:${CAM_PASS}@$CAM:554/Streaming/Channels/101"
