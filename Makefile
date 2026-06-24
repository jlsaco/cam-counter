# ============================================================================
#  cam-counter — Makefile de arranque rapido (conteo + API/UI + cloud-sync)
#
#  Carga ./.env (creado desde .env.example) y orquesta TRES procesos:
#    - edge : supervisor de borde (captura RTSP real + Hailo + conteo) -> SQLite + /healthz
#    - api  : FastAPI + UI same-origin (sirve la UI y el video RTSP en vivo)  -> :8088
#    - sync : worker edge -> AWS (drena CrossingEvents synced=0 a DynamoDB/S3)
#
#  El edge necesita el Hailo, que el servicio legacy `hailo-personas` retiene en
#  exclusiva: `make up` BAJA el legacy primero (rollback: `make legacy-start`).
#
#  Uso tipico:   make up        # arranca todo (pide sudo para bajar el legacy)
#                make status    # ver salud + backlog de sync
#                make down      # parar edge + api + sync
#  Ayuda:        make help
# ============================================================================

.RECIPEPREFIX = >
.DEFAULT_GOAL := help

# --- Carga de configuracion (.env) ------------------------------------------
-include .env
export

REPO     := $(CURDIR)
RUN_DIR  := $(REPO)/.run
VENV_PY  := $(REPO)/.venv/bin/python
SYS_PY   := /usr/bin/python3
LEGACY   := hailo-personas

# python para el edge: necesita cv2 + hailo_platform (estan en el python del
# sistema, NO en el venv). El venv se usa para la API y el cloud-sync (boto3).
EDGE_PY  := $(SYS_PY)

.PHONY: help up down restart status logs edge api sync rtsp \
        legacy-stop legacy-start install build-ui clean validate-contracts

# venv aislado del gate de contratos (no contamina el .venv del producto).
CONTRACTS_VENV := $(REPO)/.venv-contracts
CONTRACTS_PY   := $(CONTRACTS_VENV)/bin/python

help:
> @echo "cam-counter — targets:"
> @echo "  make up           Bajar legacy + arrancar edge + api/UI + cloud-sync  [pide sudo]"
> @echo "  make down         Parar edge + api + sync (no rearranca el legacy)"
> @echo "  make restart      down + up"
> @echo "  make status       Estado de procesos + salud + backlog de sync"
> @echo "  make logs         Seguir logs de edge + api + sync (Ctrl-C para salir)"
> @echo "  make edge         Arrancar SOLO el edge en primer plano (debug)"
> @echo "  make api          Arrancar SOLO la api/UI en primer plano (debug)"
> @echo "  make sync         Arrancar SOLO el cloud-sync en primer plano (debug)"
> @echo "  make rtsp         Reactivar el RTSP de la camara (si se apago)"
> @echo "  make legacy-stop  Parar el servicio legacy hailo-personas        [sudo]"
> @echo "  make legacy-start Rearrancar el servicio legacy hailo-personas   [sudo]"
> @echo "  make validate-contracts  Gate de contratos: valida ejemplos vs contracts/ (falla cerrado)"
> @echo "  make install      Instalar deps (venv: edge+api+boto3) y construir la UI"
> @echo "  make build-ui     Reconstruir solo la SPA (v1/ui/dist)"
> @echo "  make clean        Borrar .run/ (pids/logs)"
> @echo ""
> @echo "  URLs:  UI/API http://<ip-pi>:$(CAMCOUNTER_PORT)/   |   edge /healthz :$(CAMCOUNTER_HEALTHZ_PORT)"

# --- Arranque / parada del stack nuevo --------------------------------------
up: $(RUN_DIR)
> @echo ">> Bajando el servicio legacy ($(LEGACY)) para liberar el Hailo..."
> @sudo systemctl stop $(LEGACY) || true
> @echo ">> Arrancando edge (captura RTSP real + Hailo + conteo)..."
> @cd $(REPO)/v1/edge && OPENCV_FFMPEG_CAPTURE_OPTIONS='rtsp_transport;tcp' \
        PYTHONPATH=$(REPO)/v1/edge nohup $(EDGE_PY) -m cam_counter_edge.app \
        > $(RUN_DIR)/edge.log 2>&1 & echo $$! > $(RUN_DIR)/edge.pid
> @echo ">> Arrancando api + UI (FastAPI/Uvicorn, video RTSP en vivo)..."
> @cd $(REPO)/v1/api && nohup $(REPO)/v1/api/run_api.sh \
        > $(RUN_DIR)/api.log 2>&1 & echo $$! > $(RUN_DIR)/api.pid
> @if [ "$(CAMCOUNTER_SYNC_ENABLED)" = "1" ]; then \
        echo ">> Arrancando cloud-sync (edge -> AWS)..."; \
        cd $(REPO)/v1/edge && nohup $(VENV_PY) -m cam_counter_edge.sync_runner \
          > $(RUN_DIR)/sync.log 2>&1 & echo $$! > $(RUN_DIR)/sync.pid; \
    else echo ">> cloud-sync DESHABILITADO (CAMCOUNTER_SYNC_ENABLED!=1)"; fi
> @sleep 6
> @$(MAKE) --no-print-directory status

down:
> @echo ">> Parando api + edge + sync..."
> @-[ -f $(RUN_DIR)/api.pid ]  && kill $$(cat $(RUN_DIR)/api.pid)  2>/dev/null || true
> @-[ -f $(RUN_DIR)/edge.pid ] && kill $$(cat $(RUN_DIR)/edge.pid) 2>/dev/null || true
> @-[ -f $(RUN_DIR)/sync.pid ] && kill $$(cat $(RUN_DIR)/sync.pid) 2>/dev/null || true
> @-pkill -f 'uvicorn app:app' 2>/dev/null || true
> @-pkill -f 'cam_counter_edge.app' 2>/dev/null || true
> @-pkill -f 'cam_counter_edge.sync_runner' 2>/dev/null || true
> @rm -f $(RUN_DIR)/api.pid $(RUN_DIR)/edge.pid $(RUN_DIR)/sync.pid
> @echo ">> Parado. (El legacy NO se rearranca solo: usa 'make legacy-start' si lo quieres.)"

restart: down
> @sleep 1
> @$(MAKE) --no-print-directory up

status:
> @echo "=== procesos ==="
> @if [ -f $(RUN_DIR)/edge.pid ] && kill -0 $$(cat $(RUN_DIR)/edge.pid) 2>/dev/null; then \
        echo "  edge : UP (pid $$(cat $(RUN_DIR)/edge.pid))"; else echo "  edge : DOWN"; fi
> @if [ -f $(RUN_DIR)/api.pid ] && kill -0 $$(cat $(RUN_DIR)/api.pid) 2>/dev/null; then \
        echo "  api  : UP (pid $$(cat $(RUN_DIR)/api.pid))"; else echo "  api  : DOWN"; fi
> @if [ -f $(RUN_DIR)/sync.pid ] && kill -0 $$(cat $(RUN_DIR)/sync.pid) 2>/dev/null; then \
        echo "  sync : UP (pid $$(cat $(RUN_DIR)/sync.pid))"; else echo "  sync : DOWN"; fi
> @echo "=== salud ==="
> @printf "  edge /healthz : "; curl -s -m 3 http://localhost:$(CAMCOUNTER_HEALTHZ_PORT)/healthz || echo "(sin respuesta)"; echo
> @printf "  api  /api/health : "; curl -s -m 3 http://localhost:$(CAMCOUNTER_PORT)/api/health || echo "(sin respuesta)"; echo
> @printf "  UI   /         : "; curl -s -m 3 -o /dev/null -w "HTTP %{http_code}\n" http://localhost:$(CAMCOUNTER_PORT)/ || echo "(sin respuesta)"
> @printf "  cloud-sync backlog (eventos synced=0): "; cd $(REPO)/v1/edge && CAMCOUNTER_DB_PATH='$(CAMCOUNTER_DB_PATH)' $(VENV_PY) -c "import os;from cam_counter_edge import Store;print(len(Store(os.environ['CAMCOUNTER_DB_PATH']).get_unsynced_events(100000)))" 2>/dev/null || echo "?"
> @IP=$$(hostname -I | awk '{print $$1}'); echo "  -> abre en el navegador:  http://$$IP:$(CAMCOUNTER_PORT)/"

logs:
> @tail -n 40 -F $(RUN_DIR)/edge.log $(RUN_DIR)/api.log $(RUN_DIR)/sync.log

# --- Targets en primer plano (debug) ----------------------------------------
edge:
> @echo ">> edge en primer plano (Ctrl-C para parar). Requiere el Hailo libre (make legacy-stop)."
> cd $(REPO)/v1/edge && OPENCV_FFMPEG_CAPTURE_OPTIONS='rtsp_transport;tcp' \
        PYTHONPATH=$(REPO)/v1/edge $(EDGE_PY) -m cam_counter_edge.app

api:
> @echo ">> api/UI en primer plano (Ctrl-C para parar). UI en http://<pi>:$(CAMCOUNTER_PORT)/"
> cd $(REPO)/v1/api && $(REPO)/v1/api/run_api.sh

sync:
> @echo ">> cloud-sync en primer plano (Ctrl-C para parar). Drena eventos a AWS."
> cd $(REPO)/v1/edge && $(VENV_PY) -m cam_counter_edge.sync_runner

# --- RTSP de la camara ------------------------------------------------------
rtsp:
> @echo ">> Reactivando RTSP en la camara..."
> @CAM_PASS=$$(printf '%s' "$(CAMCOUNTER_RTSP_URL)" | sed -E 's#.*//[^:]+:([^@]+)@.*#\1#') \
        bash $(REPO)/v1/rtsp-enable/rtsp_enable_final.sh

# --- Servicio legacy --------------------------------------------------------
legacy-stop:
> @sudo systemctl stop $(LEGACY) && echo ">> $(LEGACY) parado."

legacy-start:
> @sudo systemctl start $(LEGACY) && echo ">> $(LEGACY) arrancado (retoma el Hailo y :8080)."

# --- Setup ------------------------------------------------------------------
install:
> @test -d $(REPO)/.venv || $(SYS_PY) -m venv $(REPO)/.venv
> $(VENV_PY) -m pip install -e $(REPO)/v1/edge
> $(VENV_PY) -m pip install -r $(REPO)/v1/api/requirements.txt
> $(VENV_PY) -m pip install boto3
> @$(MAKE) --no-print-directory build-ui

build-ui:
> cd $(REPO)/v1/ui && npm ci && npm run build

clean:
> rm -rf $(RUN_DIR)

# --- Gate de contratos (WP02) -----------------------------------------------
# Valida que el payload MQTT (= crossing_event verbatim) y el desired de la
# shadow line-config (= line_config verbatim) se ajustan a contracts/. Aislado
# en su propio venv: sólo jsonschema + pytest, NO el paquete del producto.
validate-contracts:
> @test -d $(CONTRACTS_VENV) || $(SYS_PY) -m venv $(CONTRACTS_VENV)
> @$(CONTRACTS_PY) -m pip install --quiet --upgrade pip
> @$(CONTRACTS_PY) -m pip install --quiet -r $(REPO)/tests/contracts/requirements.txt
> $(CONTRACTS_PY) -m pytest $(REPO)/tests/contracts -q

$(RUN_DIR):
> @mkdir -p $(RUN_DIR)
