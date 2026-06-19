#!/usr/bin/env bash
# Activa el RTSP de la cámara EZVIZ/Hikvision desde la Raspberry Pi.
# SDK nativo Hikvision (x86-64) ejecutado vía box64 (maneja el kernel de 16KB de la Pi5).
# Operación 100% local por el puerto 8000 (login admin + setServiceSwitch rtsp=1).
#
#   Uso:  bash enable_rtsp_now.sh [IP] [PUERTO] [USUARIO] [PASSWORD] [intervalo_seg]
#   Por defecto: 192.168.1.10 8000 admin RWCHBY  (one-shot)
#   Con un 5º arg (p.ej. 30) corre como demonio: revisa el 554 y lo reactiva.
set -e
cd "$(dirname "$0")"
BASE="$(pwd)"

CAM_IP="${1:-192.168.1.10}"
CAM_PORT="${2:-8000}"
CAM_USER="${3:-admin}"
CAM_PASS="${4:-RWCHBY}"
INTERVAL="${5:-}"

CP=""; for j in "$BASE"/*.jar; do CP="$CP:$j"; done; CP="${CP#:}"
ARGS=(--host="$CAM_IP" --port="$CAM_PORT" --username="$CAM_USER" --password="$CAM_PASS")
[ -n "$INTERVAL" ] && ARGS+=(--interval="$INTERVAL")

export BOX64_NOBANNER=1
# Dynarec DESACTIVADO: intérprete puro de box64. Lento pero 100% correcto;
# elimina los crashes no deterministas del JIT del JVM bajo emulación.
export BOX64_DYNAREC=0
# Forzar a box64 a EMULAR la OpenSSL x86 del SDK (no sustituir por la nativa ARM):
# sin esto, "OpenSSL Not All Function Loaded" y el handshake TLS del login falla (err 9).
export BOX64_EMULATED_LIBS="libcrypto.so.1.1,libssl.so.1.1"
export BOX64_LD_LIBRARY_PATH="$BASE/lib:$BASE/lib/HCNetSDKCom:$BASE/x64root/usr/lib/x86_64-linux-gnu:$BASE/x64root/usr/lib64"

echo "[*] Activando RTSP en $CAM_IP:$CAM_PORT (usuario $CAM_USER)${INTERVAL:+  [demonio cada ${INTERVAL}s]}"
box64 "$BASE/x64root/jre/bin/java" \
  -Dfile.encoding=UTF8 -Djna.library.path="$BASE/lib" \
  -Djdk.reflect.useDirectMethodHandle=false \
  -XX:+UnlockExperimentalVMOptions -XX:+UseEpsilonGC -Xmx768m \
  $EZVIZ_JAVA_OPTS \
  -cp "$CP" fr.javatic.ezvizEnableRtsp.Main "${ARGS[@]}"

if [ -z "$INTERVAL" ]; then
  sleep 2
  if (echo >/dev/tcp/"$CAM_IP"/554) >/dev/null 2>&1; then
    echo "✅ RTSP ACTIVO en $CAM_IP:554"
  else
    echo "⚠️  554 sigue cerrado; revisar credenciales/compatibilidad."
  fi
fi
