#!/usr/bin/env bash
# Un único intento diferido: espera a que expire el bloqueo de login, activa RTSP.
BASE=/home/pi/ezviz_rtsp; LOG=$BASE/delayed_enable.log
for wait in 2100 1800 1800; do   # ~35min, +30, +30
  sleep "$wait"
  if (echo >/dev/tcp/192.168.1.10/554) >/dev/null 2>&1; then
    echo "$(date) 554 ya abierto" >> "$LOG"; touch "$BASE/RTSP_ENABLED.flag"; exit 0; fi
  echo "$(date) intento de activación..." >> "$LOG"
  export EZVIZ_PROBE_LIST=$'PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json|||{"servicesSwitch":{"rtsp":1,"upnp":1,"web":1,"hiksdk":1}}'
  bash "$BASE/enable_rtsp_now.sh" >> "$LOG" 2>&1
  sleep 2
  if (echo >/dev/tcp/192.168.1.10/554) >/dev/null 2>&1; then
    echo "$(date) ✅ RTSP ACTIVADO" >> "$LOG"; touch "$BASE/RTSP_ENABLED.flag"; exit 0; fi
done
echo "$(date) no se logró; reiniciar la cámara" >> "$LOG"
