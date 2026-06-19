#!/usr/bin/env bash
# Vigila un power-cycle de la cámara: detecta cuando se va (apagas) y vuelve (enciendes),
# y en cuanto arranca con slots de sesión frescos, dispara UN ÚNICO login+PUT para
# activar RTSP agarrando el primer slot. Resuelve la IP por MAC.
BASE=/home/pi/ezviz_rtsp
LOG=$BASE/reboot_watcher.log
MAC="ac:1c:26"

find_ip(){ for i in $(seq 1 254); do ping -c1 -W1 192.168.1.$i >/dev/null 2>&1 & done; wait
           ip neigh | grep -i "$MAC" | grep -oE '192\.168\.1\.[0-9]+' | head -1; }
up(){ local ip="$1"; [ -n "$ip" ] && timeout 2 bash -c "echo >/dev/tcp/$ip/8000" 2>/dev/null; }

echo "$(date) vigilante iniciado. Esperando power-cycle de la cámara (MAC $MAC)..." > "$LOG"

# Estado inicial
IP=$(find_ip); echo "$(date) cámara actual en ${IP:-?} (esperando que se APAGUE)" >> "$LOG"

# 1) Esperar a que se APAGUE (deja de responder 8000) — hasta 10 min
for n in $(seq 1 120); do
  IP=$(ip neigh | grep -i "$MAC" | grep -oE '192\.168\.1\.[0-9]+' | head -1)
  if ! up "$IP"; then echo "$(date) cámara APAGADA detectada" >> "$LOG"; break; fi
  sleep 5
done

# 2) Esperar a que VUELVA (8000 abierto) — hasta 10 min
CAM=""
for n in $(seq 1 120); do
  CAM=$(find_ip)
  if up "$CAM"; then echo "$(date) cámara ENCENDIDA en $CAM" >> "$LOG"; break; fi
  sleep 5
done
[ -z "$CAM" ] && { echo "$(date) la cámara no volvió" >> "$LOG"; exit 1; }

# 3) Dar tiempo a que el servicio SDK arranque del todo, luego UN intento
sleep 20
echo "$(date) disparando login+PUT en $CAM" >> "$LOG"
export EZVIZ_LOGINMODE=0 EZVIZ_HTTPS=0
export EZVIZ_PROBE_LIST=$'PUT /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json|||{"servicesSwitch":{"rtsp":1,"upnp":1,"web":1,"hiksdk":1}}\nGET /ISAPI/EZVIZ/IPC/System/servicesSwitch?format=json'
bash "$BASE/enable_rtsp_now.sh" "$CAM" 8000 admin RWCHBY >> "$LOG" 2>&1
sleep 3
if timeout 3 bash -c "echo >/dev/tcp/$CAM/554" 2>/dev/null; then
  echo "$(date) ✅✅✅ RTSP ACTIVADO en $CAM:554" >> "$LOG"
  echo "$CAM" > "$BASE/CAM_IP"; touch "$BASE/RTSP_ENABLED.flag"; exit 0
fi
echo "$(date) primer intento sin éxito; reintentando UNA vez tras 15s" >> "$LOG"
sleep 15
bash "$BASE/enable_rtsp_now.sh" "$CAM" 8000 admin RWCHBY >> "$LOG" 2>&1
sleep 3
if timeout 3 bash -c "echo >/dev/tcp/$CAM/554" 2>/dev/null; then
  echo "$(date) ✅✅✅ RTSP ACTIVADO en $CAM:554 (2º intento)" >> "$LOG"
  echo "$CAM" > "$BASE/CAM_IP"; touch "$BASE/RTSP_ENABLED.flag"; exit 0
fi
echo "$(date) no se logró tras el reinicio" >> "$LOG"; exit 1
