#!/usr/bin/env bash
# Espera en SILENCIO a que expire el lockout de login, resuelve la IP de la cámara
# por su MAC, y hace UN ÚNICO intento de login+PUT (params 0,0, 1 try). No martillea.
BASE=/home/pi/ezviz_rtsp
LOG=$BASE/oneshot.log
MAC="ac:1c:26"
WAIT="${1:-2000}"

echo "$(date) esperando ${WAIT}s en silencio para que expire el lockout..." > "$LOG"
sleep "$WAIT"

# Resolver IP actual de la cámara por MAC
for i in $(seq 1 254); do ping -c1 -W1 192.168.1.$i >/dev/null 2>&1 & done; wait
IP=$(ip neigh | grep -i "$MAC" | grep -oE '192\.168\.1\.[0-9]+' | head -1)
if [ -z "$IP" ]; then echo "$(date) no encuentro la cámara (MAC $MAC) en la red" >> "$LOG"; exit 1; fi
echo "$(date) cámara en $IP; intento único de activación" >> "$LOG"

if timeout 3 bash -c "echo >/dev/tcp/$IP/554" 2>/dev/null; then
  echo "$(date) 554 ya abierto" >> "$LOG"; touch "$BASE/RTSP_ENABLED.flag"; echo "$IP" > "$BASE/CAM_IP"; exit 0
fi

export EZVIZ_LOGINMODE=0 EZVIZ_HTTPS=0
export EZVIZ_PROBE_LIST=$'PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json|||{"servicesSwitch":{"rtsp":1,"upnp":1,"web":1,"hiksdk":1}}\nGET /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json'
bash "$BASE/enable_rtsp_now.sh" "$IP" 8000 admin RWCHBY >> "$LOG" 2>&1
sleep 3
if timeout 3 bash -c "echo >/dev/tcp/$IP/554" 2>/dev/null; then
  echo "$(date) ✅ RTSP ACTIVADO (554 abierto) en $IP" >> "$LOG"
  touch "$BASE/RTSP_ENABLED.flag"; echo "$IP" > "$BASE/CAM_IP"; exit 0
fi
echo "$(date) intento sin éxito; el lockout puede necesitar más tiempo o un reinicio de cámara" >> "$LOG"
exit 1
