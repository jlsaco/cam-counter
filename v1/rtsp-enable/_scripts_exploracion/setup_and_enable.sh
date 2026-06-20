#!/usr/bin/env bash
# Activa el RTSP de la cámara EZVIZ/Hikvision desde la propia Raspberry Pi.
# Usa el SDK nativo Hikvision (x86-64) ejecutado vía qemu-user-static sobre ARM64.
# Operación local por el puerto 8000 (login admin + setServiceSwitch rtsp=1). Sin nube.
set -e

# Credencial por entorno $CAM_PASS o fichero gitignored rtsp-enable/CAM_PASS (sin literal).
source "$(dirname "$0")/../_lib_credentials.sh"

CAM_IP="${1:-192.168.1.10}"
CAM_PORT="${2:-8000}"
CAM_USER="${3:-admin}"
CAM_PASS_ARG="${4:-}"
if [ -n "$CAM_PASS_ARG" ]; then
  CAM_PASS="$CAM_PASS_ARG"
else
  CAM_PASS="$(resolve_cam_pass)" || exit 1
fi
DIR="/home/pi/ezviz_rtsp"
JRE_AMD64="/usr/lib/jvm/java-21-openjdk-amd64/bin/java"

echo "[1/3] Preparando runtime x86-64 (qemu + JRE amd64)..."
if [ ! -x "$JRE_AMD64" ]; then
  sudo dpkg --add-architecture amd64
  sudo apt-get update
  sudo apt-get install -y qemu-user-static openjdk-21-jre-headless:amd64
fi

echo "[2/3] Activando RTSP en $CAM_IP:$CAM_PORT (usuario $CAM_USER)..."
CP="$DIR/ezviz-enable-rtsp-1.0-SNAPSHOT.jar"
for j in "$DIR"/*.jar; do [ "$j" != "$CP" ] && CP="$CP:$j"; done
"$JRE_AMD64" -Dfile.encoding=UTF8 -Djna.library.path="$DIR/lib" \
  -cp "$CP" fr.javatic.ezvizEnableRtsp.Main \
  --host="$CAM_IP" --port="$CAM_PORT" --username="$CAM_USER" --password="$CAM_PASS"

echo "[3/3] Verificando puerto 554..."
sleep 2
if (echo >/dev/tcp/"$CAM_IP"/554) >/dev/null 2>&1; then
  echo "✅ RTSP ACTIVO en $CAM_IP:554"
else
  echo "⚠️  554 sigue cerrado; revisar credenciales o compatibilidad del firmware."
fi
