#!/usr/bin/env bash
# Activa RTSP en la cámara EZVIZ. Resuelve IP por MAC. Solo actúa si 554 está cerrado.
BASE="$(cd "$(dirname "$0")" && pwd)"; MAC="ac:1c:26"
for i in $(seq 1 254); do ping -c1 -W1 192.168.1.$i >/dev/null 2>&1 & done; wait
CAM=$(ip neigh | grep -i "$MAC" | grep -oE '192\.168\.1\.[0-9]+' | head -1)
[ -z "$CAM" ] && { echo "$(date) camara no encontrada"; exit 1; }
echo "$CAM" > "$BASE/CAM_IP"
if timeout 3 bash -c "echo >/dev/tcp/$CAM/554" 2>/dev/null; then
  echo "$(date) 554 ya abierto en $CAM"; exit 0
fi
export EZVIZ_LOGINMODE=0 EZVIZ_HTTPS=0 EZVIZ_LOGIN_RETRIES=20 EZVIZ_BODY_IN_URL=1
export EZVIZ_PROBE_LIST=$'PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json|||{"servicesSwitch":{"rtsp":1,"upnp":1,"web":1,"hiksdk":1}}'
# La credencial la resuelve enable_rtsp_now.sh por entorno $CAM_PASS o fichero gitignored
# (rtsp-enable/CAM_PASS); no se pasa ningún literal por argumento.
bash "$BASE/enable_rtsp_now.sh" "$CAM" 8000 admin
sleep 2
timeout 3 bash -c "echo >/dev/tcp/$CAM/554" 2>/dev/null && echo "$(date) RTSP activado en $CAM" || echo "$(date) no se pudo activar"
