"""Builders de claves DynamoDB + validación de slugs para la API de flota (read-only).

Refleja EXACTAMENTE las convenciones de ``CLAUDE.md`` §3/§7 y de los módulos de tabla
(``events-table`` / ``device-registry``), de modo que esta API LEA con las MISMAS claves con las
que el camino edge→cloud / MQTT ESCRIBE:

- Eventos  (``cam-counter-events``)  : ``PK = CAM#{site}#{device}#{camera}``,
                                       ``SK = TS#{ts_event_ms:013d}#{event_id}``,
                                       ``GSI1PK = SITE#{site}``.
- Devices  (``cam-counter-devices``) : ``PK = DEVICE#{device_id}``,
                                       ``GSI1PK = CHANNEL#{release_channel}``,
                                       ``GSI1SK = DEVICE#{device_id}``.
"""

from __future__ import annotations

import re

__all__ = [
    "CHANNELS",
    "SLUG_RE",
    "camera_pk",
    "channel_gsi1pk",
    "device_pk",
    "valid_slug",
]

# Slug ASCII minúscula (CLAUDE.md §3): sin '#' (delimita claves DynamoDB) ni '/' (delimita rutas
# S3). Validar ANTES de construir cualquier clave (evita inyección en PK/SK).
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# Canales de release válidos (contrato device_registry_item: enum canary|stable). Listar /devices
# sin filtro consulta el GSI1 de AMBOS canales.
CHANNELS = ("stable", "canary")


def valid_slug(value: object) -> bool:
    """True si ``value`` es un slug ASCII minúscula que cumple ``SLUG_RE``."""
    return isinstance(value, str) and SLUG_RE.match(value) is not None


def device_pk(device_id: str) -> str:
    """PK del item de registro de un dispositivo: ``DEVICE#{device_id}``."""
    return f"DEVICE#{device_id}"


def channel_gsi1pk(channel: str) -> str:
    """GSI1PK por canal del registro de dispositivos: ``CHANNEL#{release_channel}``."""
    return f"CHANNEL#{channel}"


def camera_pk(site_id: str, device_id: str, camera_id: str) -> str:
    """PK de la partición de eventos de una cámara: ``CAM#{site}#{device}#{camera}``."""
    return f"CAM#{site_id}#{device_id}#{camera_id}"
