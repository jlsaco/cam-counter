# syntax=docker/dockerfile:1
#
# docker/edge.Dockerfile — imagen de PRODUCCIÓN del servicio `edge` (WP17).
#
# Evoluciona el PROTOTIPO del SPIKE WP09 (#45): aquel demostró —en una Pi5 ARM64
# real (GO/NO-GO = GO)— que el pipeline edge (HailoRT + cv2 + ffmpeg) corre dentro
# de un contenedor abriendo /dev/hailo0 SIN --privileged. Esta imagen aplica ese GO:
# misma estrategia de runtime/driver, pero el ENTRYPOINT ya NO es el probe del
# spike sino el SUPERVISOR de conteo (cam_counter_edge.app) + sync (sync_dispatch).
#
# La IMAGEN es GENÉRICA (sin identidad ni secretos): lo por-device es el `.env`
# (CAMCOUNTER_*) y el volumen `certs` (montados en runtime, NUNCA horneados ni
# commiteados). El runtime nativo (systemd) sigue soportado como PLAN B.
#
# Decisiones clave heredadas del PoC (justificadas en docs/docker-device.md):
#
#  1) BASE = Debian *trixie* (NO bookworm). El issue pedía "bookworm", pero el HOST
#     de producción es Debian 13 (trixie) y su driver HailoRT (módulo de kernel
#     hailo_pci 4.23.0) viene del repo trixie. HailoRT exige que el RUNTIME
#     userspace case EXACTAMENTE con el driver del kernel del host. Basar en trixie
#     + instalar el .deb del propio host garantiza ese match; bookworm reintroduce
#     el riesgo de desajuste que este trabajo existe para eliminar.
#  2) HailoRT se instala desde el .deb del HOST (staged en docker/debs/ por
#     docker/stage-debs.sh), NO desde el repo apt de RPi (en trixie `sqv` rechaza
#     su firma SHA1). Bits idénticos al host => match duro driver==runtime. El build
#     PINNEA por ARG y FALLA si la versión instalada no coincide (fail-closed).
#  3) NO se instala el driver de kernel: vive en el HOST. El contenedor sólo trae
#     runtime userspace + binding python; /dev/hailo0 se inyecta con `--device`.
#
# CONVENCIÓN DE TAG (WP17): la imagen se etiqueta con la versión de HailoRT para
# que el operador no mezcle imagen y driver:  cam-counter-edge:<ver>-hrt4.23.0-arm64
# (CAMCOUNTER_IMAGE_TAG en el .env). El ARG HAILORT_VERSION DEBE casar con ese tag.
#
# Build (DEBE correr en la Pi5, ARM64 real — NO en CI x86/qemu; el camino DMA del
# Hailo no es fiable bajo qemu, ver nota [MEDIA] del revisor):
#     docker/stage-debs.sh                                   # materializa docker/debs/*.deb
#     docker build -f docker/edge.Dockerfile -t cam-counter-edge:dev-hrt4.23.0-arm64 .
# (el contexto de build es la RAÍZ del repo: la imagen necesita v1/edge además de los .deb)

ARG BASE_IMAGE=debian:trixie-slim
FROM ${BASE_IMAGE}

# Versión de HailoRT esperada; DEBE coincidir con el driver del kernel host.
# Verificar en el host con:  modinfo hailo_pci | grep ^version
ARG HAILORT_VERSION=4.23.0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    # El paquete cam_counter_edge se importa desde aquí (sin pip install: evita
    # PEP-668 en el python del sistema y mantiene la imagen reproducible).
    PYTHONPATH=/opt/cam-counter/edge

# Fail-closed: este contenedor no tiene sentido fuera de ARM64 (camino DMA Hailo).
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    if [ "$arch" != "arm64" ]; then \
        echo "ERROR: edge.Dockerfile DEBE construirse en ARM64 real (Pi5). Detectado: $arch (probable qemu/x86). Abortando." >&2; \
        exit 1; \
    fi

# 1) Dependencias de runtime desde Debian trixie (firmado):
#    - cv2 + ffmpeg + numpy + pillow : pipeline de captura/inferencia/clips.
#    - paho-mqtt + boto3             : transporte de sync (modo iot a IoT Core; boto3
#                                       sube clips vía el IoT Credential Provider).
#    Se usan paquetes apt (no pip) para evitar PEP-668 y compilar nada en el Pi.
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-numpy \
        python3-opencv \
        python3-pil \
        python3-paho-mqtt \
        python3-boto3 \
        ffmpeg; \
    rm -rf /var/lib/apt/lists/*

# 2) HailoRT (runtime userspace) + binding python desde los .deb del HOST.
#    `apt-get install ./debs/*.deb` instala los .deb locales y resuelve sus
#    dependencias desde Debian trixie. Bits idénticos al host => match garantizado.
COPY docker/debs/ /tmp/debs/
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends /tmp/debs/*.deb; \
    rm -rf /tmp/debs /var/lib/apt/lists/*

# 3) VERIFICACIÓN DE PIN en build (fail-closed): la versión instalada de hailort
#    debe ser EXACTAMENTE la pedida (acepta sufijo de revisión -N de Debian).
RUN set -eux; \
    installed="$(dpkg-query -W -f='${Version}' hailort)"; \
    echo "hailort instalado en el contenedor: ${installed} (esperado: ${HAILORT_VERSION})"; \
    case "${installed}" in \
        "${HAILORT_VERSION}"|"${HAILORT_VERSION}-"*) : ;; \
        *) echo "ERROR: hailort ${installed} != ${HAILORT_VERSION} pedido. Abortando." >&2; exit 1 ;; \
    esac; \
    python3 -c "import hailo_platform; print('hailo_platform OK', hailo_platform.__version__)"; \
    python3 -c "import cv2; print('cv2 OK', cv2.__version__)"; \
    python3 -c "import paho.mqtt.client; print('paho OK')"; \
    python3 -c "import boto3; print('boto3 OK', boto3.__version__)"

# 4) Código del borde (paquete genérico, SIN identidad/secretos) + entrypoint +
#    probe del spike (sigue disponible como diagnóstico: `--entrypoint python3 …`).
COPY v1/edge/ /opt/cam-counter/edge/
COPY docker/hailo_probe.py /opt/cam-counter/hailo_probe.py
COPY docker/edge-entrypoint.sh /opt/cam-counter/edge-entrypoint.sh
RUN set -eux; \
    chmod +x /opt/cam-counter/edge-entrypoint.sh; \
    python3 -c "import cam_counter_edge, cam_counter_edge.healthcheck; print('cam_counter_edge OK')"

# Canon de entorno CAMCOUNTER_* (ver docs/docker-device.md). El HEF del host se
# monta read-only en /usr/share/hailo-models (evita una imagen gigante).
ENV CAMCOUNTER_HEF_PATH=/usr/share/hailo-models/yolov8s_h8.hef \
    CAMCOUNTER_DB_PATH=/var/lib/cam-counter/cam-counter.db \
    CAMCOUNTER_HEALTHZ_HOST=0.0.0.0 \
    CAMCOUNTER_HEALTHZ_PORT=8081

# HEALTHCHECK del contenedor: sonda barata a /healthz del supervisor (liveness de
# producto). Usa el mismo módulo que el boot fail-closed (sin curl en la imagen).
HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD python3 -m cam_counter_edge.healthcheck http || exit 1

WORKDIR /opt/cam-counter/edge
ENTRYPOINT ["/opt/cam-counter/edge-entrypoint.sh"]
