#!/usr/bin/env bash
# edge-entrypoint.sh — ENTRYPOINT del contenedor `edge` (WP17, dockerización).
#
# Orquesta el proceso de borde dentro del contenedor en este orden:
#   1) VALIDACIÓN FAIL-CLOSED AL BOOT: corre `cam_counter_edge.healthcheck boot`,
#      que verifica slugs, y —en transporte iot— el canon del thing-name, el
#      endpoint/region y el material mTLS (cert/key/CA legibles, llave 0600). Si
#      algo falla, ABORTA (exit !=0) en vez de arrancar mudo y reintentar para
#      siempre. El restart-policy de compose hará backoff visible.
#   2) SYNC en segundo plano: `cam_counter_edge.sync_dispatch` (en modo iot =
#      publicador MQTT a IoT Core; en direct = boto3). Best-effort, edge-first.
#   3) SUPERVISOR en primer plano (exec): `cam_counter_edge.app` (captura + Hailo
#      + conteo + clips + /healthz). Es el PID 1 efectivo: si muere, el
#      contenedor muere y compose lo reinicia.
#
# Diseño edge-first: el conteo (supervisor) es el proceso crítico y va al frente;
# el sync es accesorio y va detrás. Si el sync muere, el conteo NO se cae (sigue
# persistiendo en SQLite local; un reinicio del contenedor lo recupera).
set -euo pipefail

log() { echo "[edge-entrypoint] $*" >&2; }

# --- 1) fail-closed boot ------------------------------------------------------
log "validación fail-closed al boot…"
if ! python3 -m cam_counter_edge.healthcheck boot; then
    log "ABORTANDO: configuración inválida (ver arriba). Revisa el .env del device."
    exit 1
fi

# --- 2) sync en segundo plano (best-effort) ----------------------------------
sync_pid=""
if [ "${CAMCOUNTER_SYNC_ENABLED:-0}" = "1" ]; then
    log "arrancando sync (transporte=${CAMCOUNTER_SYNC_TRANSPORT:-direct}) en segundo plano…"
    python3 -m cam_counter_edge.sync_dispatch &
    sync_pid=$!
else
    log "CAMCOUNTER_SYNC_ENABLED!=1: sync desactivado (sólo conteo local)."
fi

# Propaga SIGTERM/SIGINT al sync para un apagado limpio del contenedor.
shutdown() {
    log "señal recibida; parando…"
    [ -n "$sync_pid" ] && kill -TERM "$sync_pid" 2>/dev/null || true
}
trap shutdown TERM INT

# --- 3) supervisor en primer plano (proceso crítico) -------------------------
log "arrancando supervisor de conteo (cam_counter_edge.app)…"
exec python3 -m cam_counter_edge.app
