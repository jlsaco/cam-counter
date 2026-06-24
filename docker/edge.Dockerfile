# syntax=docker/dockerfile:1
#
# docker/edge.Dockerfile — PROTOTIPO del contenedor edge para el SPIKE WP09 (issue #45).
#
# Objetivo del spike: probar que el pipeline edge (HailoRT + cv2 + ffmpeg) puede
# correr DENTRO de un contenedor en una Raspberry Pi 5 (ARM64 real), abriendo
# /dev/hailo0 SIN --privileged y ejecutando una inferencia real sobre el Hailo-8.
#
# Decisiones clave (justificadas en docs/poc-hailo-docker.md):
#
#  1) BASE = Debian *trixie* (NO bookworm). El issue pedia "bookworm", pero el
#     HOST de produccion es Debian 13 (trixie) y su driver HailoRT (modulo de
#     kernel hailo_pci 4.23.0) viene del repo `archive.raspberrypi.com/debian
#     trixie`. HailoRT exige que el RUNTIME userspace case EXACTAMENTE con el
#     driver del kernel del host. Basar en trixie + instalar el .deb del propio
#     host garantiza ese match. Bookworm reintroduciria el riesgo de desajuste
#     que este spike existe para eliminar.
#
#  2) HailoRT se instala desde el .deb del HOST (staged en docker/debs/ por
#     docker/stage-debs.sh), NO desde el repo apt de RPi. Motivo: en trixie,
#     `sqv` rechaza la firma SHA1 de la clave del repo RPi (inseguro desde
#     2026-02-01). El .deb local evita el problema de firma Y da bits IDENTICOS
#     a los del host (garantia dura HailoRT==driver). El build PINNEA por ARG y
#     FALLA si la version instalada no coincide (fail-closed).
#
#  3) NO se instala el driver (hailort-pcie-driver): el driver vive en el KERNEL
#     DEL HOST. El contenedor solo trae runtime userspace + binding python; el
#     nodo /dev/hailo0 se inyecta en runtime con `--device` (sin --privileged).
#
# Build (DEBE correr en la Pi5, ARM64 real — NO en CI x86/qemu):
#     docker/stage-debs.sh        # materializa docker/debs/*.deb desde el host
#     docker build -f docker/edge.Dockerfile -t cam-counter-edge:poc docker/
# (o simplemente:  docker/run-probe.sh   que encadena ambos pasos)

ARG BASE_IMAGE=debian:trixie-slim
FROM ${BASE_IMAGE}

# Version de HailoRT esperada; DEBE coincidir con el driver del kernel host.
# Verificar en el host con:  modinfo hailo_pci | grep ^version
ARG HAILORT_VERSION=4.23.0

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1

# Fail-closed: este contenedor no tiene sentido fuera de ARM64 (camino DMA Hailo).
RUN set -eux; \
    arch="$(dpkg --print-architecture)"; \
    if [ "$arch" != "arm64" ]; then \
        echo "ERROR: edge.Dockerfile DEBE construirse en ARM64 real (Pi5). Detectado: $arch (probable qemu/x86). Abortando." >&2; \
        exit 1; \
    fi

# 1) Dependencias de runtime (cv2 + ffmpeg + numpy) desde Debian trixie (firmado).
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends \
        python3 \
        python3-numpy \
        python3-opencv \
        ffmpeg; \
    rm -rf /var/lib/apt/lists/*

# 2) HailoRT (runtime userspace) + binding python desde los .deb del HOST.
#    `apt-get install ./debs/*.deb` instala los .deb locales y resuelve sus
#    dependencias desde Debian trixie. Bits identicos al host => match garantizado.
COPY debs/ /tmp/debs/
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends /tmp/debs/*.deb; \
    rm -rf /tmp/debs /var/lib/apt/lists/*

# 3) VERIFICACION DE PIN en build (fail-closed): la version instalada de hailort
#    debe ser EXACTAMENTE la pedida (acepta sufijo de revision -N de Debian).
RUN set -eux; \
    installed="$(dpkg-query -W -f='${Version}' hailort)"; \
    echo "hailort instalado en el contenedor: ${installed} (esperado: ${HAILORT_VERSION})"; \
    case "${installed}" in \
        "${HAILORT_VERSION}"|"${HAILORT_VERSION}-"*) : ;; \
        *) echo "ERROR: hailort ${installed} != ${HAILORT_VERSION} pedido. Abortando." >&2; exit 1 ;; \
    esac; \
    python3 -c "import hailo_platform; print('hailo_platform OK', hailo_platform.__version__)"; \
    python3 -c "import cv2; print('cv2 OK', cv2.__version__)"

# 4) Probe del spike: abre /dev/hailo0 e infiere un frame real.
COPY hailo_probe.py /opt/cam-counter/hailo_probe.py

# Canon de entorno CAMCOUNTER_* (ver docs/naming-standard.md). El HEF del host
# se monta read-only en /usr/share/hailo-models (evita imagen gigante).
ENV CAMCOUNTER_HEF_PATH=/usr/share/hailo-models/yolov8s_h8.hef \
    CAMCOUNTER_PROBE_IMAGE=""

WORKDIR /opt/cam-counter
# Por defecto, ejecuta el probe. En produccion (WP17) el ENTRYPOINT seria el
# supervisor del pipeline edge; aqui basta con demostrar el acceso al Hailo.
ENTRYPOINT ["python3", "/opt/cam-counter/hailo_probe.py"]
