#!/usr/bin/env bash
# Reintenta activar el RTSP de la cámara a cadencia tranquila hasta que lo logre.
# Pensado para correr en background mientras la cámara se recupera (drena sesiones).
BASE=/home/pi/ezviz_rtsp
LOG=$BASE/auto_enable.log
FLAG=$BASE/RTSP_ENABLED.flag
INTERVAL="${1:-180}"
MAX="${2:-12}"

echo "$(date) inicio auto-enable (cada ${INTERVAL}s, max ${MAX} intentos)" > "$LOG"
for i in $(seq 1 "$MAX"); do
  # ¿ya está abierto el 554?
  if (echo >/dev/tcp/192.168.1.10/554) >/dev/null 2>&1; then
    echo "$(date) 554 YA ABIERTO" >> "$LOG"; touch "$FLAG"; exit 0
  fi
  echo "$(date) intento $i: activando RTSP..." >> "$LOG"
  export EZVIZ_PROBE_LIST=$'PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json|||{"servicesSwitch":{"rtsp":1,"upnp":1,"web":1,"hiksdk":1}}\nGET /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json'
  bash "$BASE/enable_rtsp_now.sh" >> "$LOG" 2>&1
  sleep 2
  if (echo >/dev/tcp/192.168.1.10/554) >/dev/null 2>&1; then
    echo "$(date) ✅ RTSP ACTIVADO (554 abierto) en intento $i" >> "$LOG"
    touch "$FLAG"; exit 0
  fi
  echo "$(date) intento $i sin éxito; esperando ${INTERVAL}s" >> "$LOG"
  sleep "$INTERVAL"
done
echo "$(date) agotados ${MAX} intentos; la cámara probablemente necesita reinicio físico" >> "$LOG"
exit 1
