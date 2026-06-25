# syntax=docker/dockerfile:1
#
# edge.Dockerfile — PROTOTIPO de spike (WP09 / IOT-45): contenedor edge con HailoRT
# pinneado EXACTAMENTE a la versión del driver PCIe del host. NO es la imagen de
# producción; es el artefacto del PoC Hailo-en-Docker (ver docs/poc-hailo-docker.md).
#
# DECISIÓN DE BASE (corrige el enunciado original):
#   El issue pedía "Bookworm". En el host REAL (Raspberry Pi 5) HailoRT y su binding
#   Python (`python3-hailort`) vienen del repo `archive.raspberrypi.com` para
#   **Debian 13 trixie / Python 3.13**. Un base bookworm (Python 3.11) NO podría
#   instalar ese binding (ABI de Python distinta). Por eso la base es **trixie-slim**:
#   debe coincidir con la libc/python del host para que el .so de HailoRT cargue.
#
# INVARIANTE DURA: la versión de HailoRT del contenedor == la del driver del host.
#   En este host: hailort 4.23.0 / python3-hailort 4.23.0-1 / driver+fw 4.23.0.
#   Se fija con ARG para que un bump del host obligue a re-pinnear conscientemente.
#
# El driver del kernel (`hailo_pci`) y el firmware viven en el HOST, no en la imagen:
# el contenedor sólo trae la librería de espacio de usuario + binding Python y abre
# el `/dev/hailo0` mapeado. NO se instala `hailort-pcie-driver` dentro del contenedor.

FROM debian:trixie-slim

# Versión única a pinnear: cambiarla obliga a re-validar contra el driver del host.
ARG HAILORT_VERSION=4.23.0
ARG PY_HAILORT_VERSION=4.23.0-1

ENV DEBIAN_FRONTEND=noninteractive \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1

# 1) Repo de Raspberry Pi (misma fuente que el host) + keyring PÚBLICO copiado del
#    host vía el contexto de build (lo stagea docker/run-poc.sh; es gitignored).
#    El keyring es una clave de archivo PÚBLICA, no un secreto.
COPY raspberrypi-archive-keyring.pgp /usr/share/keyrings/raspberrypi-archive-keyring.pgp
RUN set -eux; \
    apt-get update; \
    apt-get install -y --no-install-recommends ca-certificates; \
    printf 'Types: deb\nURIs: http://archive.raspberrypi.com/debian/\nSuites: trixie\nComponents: main\nSigned-By: /usr/share/keyrings/raspberrypi-archive-keyring.pgp\n' \
        > /etc/apt/sources.list.d/raspi.sources; \
    apt-get update

# 2) HailoRT (userspace) PINNEADO + binding Python + cv2/ffmpeg/numpy.
#    Sin --install-recommends para no arrastrar el metapaquete hailo-all completo.
RUN set -eux; \
    apt-get install -y --no-install-recommends \
        "hailort=${HAILORT_VERSION}" \
        "python3-hailort=${PY_HAILORT_VERSION}" \
        python3 \
        python3-numpy \
        python3-opencv \
        ffmpeg; \
    apt-get clean; \
    rm -rf /var/lib/apt/lists/*

# 3) Usuario NO-root: el contenedor abre /dev/hailo0 por pertenencia al GID del
#    device (--group-add en el run), NUNCA por --privileged ni por correr como root.
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin edge
WORKDIR /opt/cam-counter
COPY probe_hailo.py /opt/cam-counter/probe_hailo.py
USER edge

# El HEF se MONTA en runtime (-v /usr/share/hailo-models:ro) para no hornear el
# modelo (10 MB+) en la imagen ni acoplar la imagen a un modelo concreto.
ENTRYPOINT ["python3", "/opt/cam-counter/probe_hailo.py"]
