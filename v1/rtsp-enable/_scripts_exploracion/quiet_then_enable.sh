#!/usr/bin/env bash
# Espera en SILENCIO TOTAL (sin tocar la cámara) a que expire el lock de login de
# Hikvision, luego hace UN intento. Si falla, espera otra ventana y reintenta (máx 2).
BASE=/home/pi/ezviz_rtsp
LOG=$BASE/quiet_enable.log
echo "$(date) iniciando espera silenciosa (45 min por ventana)..." > "$LOG"
for win in 1 2; do
  sleep 2700                      # 45 min de silencio total, SIN tocar la cámara
  echo "$(date) ventana $win: intento único de activación" >> "$LOG"
  export EZVIZ_PROBE_LIST=$'PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json|||{"servicesSwitch":{"rtsp":1,"upnp":1,"web":1,"hiksdk":1}}\nGET /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json'
  bash "$BASE/enable_rtsp_now.sh" >> "$LOG" 2>&1
  sleep 2
  if timeout 3 bash -c 'echo >/dev/tcp/192.168.1.10/554' 2>/dev/null; then
    echo "$(date) RTSP ACTIVADO (554 abierto)" >> "$LOG"
    touch "$BASE/RTSP_ENABLED.flag"
    exit 0
  fi
  echo "$(date) ventana $win sin exito" >> "$LOG"
done
echo "$(date) tras 2 ventanas sigue bloqueado -> probablemente requiere factory reset" >> "$LOG"
exit 1
