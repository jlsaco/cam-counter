"""Constructores de claves DynamoDB y validador acotado de ``clip_key``.

Las claves son IDÉNTICAS a las que produce el camino directo del borde
(``cam_counter_edge.sync.event_keys``) — el dual-run (directo + MQTT→Lambda) DEBE
producir la MISMA PK/SK para que el conditional put sea idempotente:

    PK     = CAM#{site_id}#{device_id}#{camera_id}
    SK     = TS#{ts_event_ms:013d}#{event_id}
    GSI1PK = SITE#{site_id}
    GSI1SK = TS#{ts_event_ms:013d}#{event_id}

Los slugs ya fueron validados por el contrato (jsonschema) antes de llegar aquí; el
``#``/``/`` no puede colarse en un slug. El validador de ``clip_key`` NO confía en el
payload: reconstruye el prefijo esperado desde la identidad del evento y exige que la
clave caiga EXACTAMENTE dentro de ``media/{site}/{device}/{camera}/.../{event_id}.<ext>``.
"""

from __future__ import annotations

import re

# Extensiones de media admitidas en la convención de claves (CLAUDE.md §7).
_ALLOWED_EXT = ("mp4", "gif", "jpg", "jpeg", "png")


def build_pk(site_id: str, device_id: str, camera_id: str) -> str:
    return f"CAM#{site_id}#{device_id}#{camera_id}"


def build_sk(ts_event_ms: int, event_id: str) -> str:
    # Zero-pad a 13 dígitos: orden lexicográfico == orden temporal hasta el año 2286.
    return f"TS#{int(ts_event_ms):013d}#{event_id}"


def build_gsi1pk(site_id: str) -> str:
    return f"SITE#{site_id}"


def looks_like_clip_key(clip_key: str, event: dict) -> bool:
    """True si ``clip_key`` cae EXACTAMENTE en el prefijo del propio evento.

    Anti-spoof: nunca confía en el payload; reconstruye el patrón a partir de
    ``site_id``/``device_id``/``camera_id``/``event_id`` del evento ya validado y
    exige fecha ``yyyy/mm/dd`` y una extensión de la whitelist. Rechaza ``..`` para
    cortar cualquier path traversal.
    """
    if not isinstance(clip_key, str) or ".." in clip_key:
        return False
    pattern = re.compile(
        r"^media/"
        + re.escape(event["site_id"])
        + "/"
        + re.escape(event["device_id"])
        + "/"
        + re.escape(event["camera_id"])
        + r"/\d{4}/\d{2}/\d{2}/"
        + re.escape(event["event_id"])
        + r"\.(?:" + "|".join(_ALLOWED_EXT) + r")$"
    )
    return bool(pattern.match(clip_key))
