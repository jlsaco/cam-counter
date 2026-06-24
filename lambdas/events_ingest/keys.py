"""Builders de claves DynamoDB + validadores anti-spoof del ``CrossingEvent``.

Refleja EXACTAMENTE las convenciones de ``CLAUDE.md`` §3/§7 y del módulo de borde
``cam_counter_edge.sync`` (mismas claves PK/SK/GSI1), de modo que el camino MQTT
(IoT Rule → esta Lambda → DynamoDB) y el camino directo edge→cloud escriban el
**mismo item idempotente** (dual-write seguro durante la migración).

Anti-spoof:
- ``recompute_event_id`` deriva el id DETERMINISTA y la Lambda EXIGE que coincida
  con el del payload (un device no puede reclamar un ``event_id`` arbitrario).
- ``validate_clip_key`` ACOTA la clave de media a la identidad del propio evento
  (``media/{site}/{device}/{camera}/YYYY/MM/DD/{event_id}.{ext}``): un payload no
  puede enlazar un clip que apunte a otra cámara/evento.
"""

from __future__ import annotations

import hashlib
import re

__all__ = [
    "SLUG_RE",
    "build_keys",
    "device_pk",
    "recompute_event_id",
    "validate_clip_key",
    "validate_slug",
]

# Slug ASCII minúscula (CLAUDE.md §3): sin '#' (delimita claves DynamoDB) ni '/'
# (delimita rutas S3). Validar ANTES de construir cualquier clave.
SLUG_RE = re.compile(r"^[a-z0-9][a-z0-9-]{1,62}$")

# event_id determinista: sha1 hex-minúscula de 40 chars (dedupe, NO cripto).
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{40}$")


def validate_slug(field: str, value: object) -> str:
    """Valida un slug de identidad. Lanza ``ValueError`` si no cumple el regex."""
    if not isinstance(value, str) or SLUG_RE.match(value) is None:
        raise ValueError(f"{field} inválido (no casa {SLUG_RE.pattern}): {value!r}")
    return value


def recompute_event_id(
    site_id: str, device_id: str, camera_id: str, track_id: str, crossing_seq: int
) -> str:
    """``event_id`` DETERMINISTA = sha1('site|device|camera|track_id|crossing_seq').

    Idéntico a ``contracts`` y al edge: misma tupla, mismo orden, mismo separador.
    El sha1 NO es criptográfico (sólo deduplicación idempotente).
    """
    raw = f"{site_id}|{device_id}|{camera_id}|{track_id}|{crossing_seq}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324 (dedupe, no crypto)


def build_keys(event: dict) -> dict[str, str]:
    """Construye PK/SK + GSI1 del evento (slugs validados ANTES).

    - ``PK  = CAM#{site_id}#{device_id}#{camera_id}``
    - ``SK  = TS#{ts_event_ms:013d}#{event_id}``
    - ``GSI1PK = SITE#{site_id}`` / ``GSI1SK = TS#{ts_event_ms:013d}#{event_id}``

    ``ts_event_ms`` es INMUTABLE por ``event_id`` (autoritativo del momento del
    cruce; NO se recomputa al publicar) para que la SK no diverja entre el camino
    directo y el MQTT y la dedupe por ``(PK, SK)`` no se rompa.
    """
    site_id = validate_slug("site_id", event["site_id"])
    device_id = validate_slug("device_id", event["device_id"])
    camera_id = validate_slug("camera_id", event["camera_id"])
    event_id = event["event_id"]
    ts = f"{int(event['ts_event_ms']):013d}"
    return {
        "PK": f"CAM#{site_id}#{device_id}#{camera_id}",
        "SK": f"TS#{ts}#{event_id}",
        "GSI1PK": f"SITE#{site_id}",
        "GSI1SK": f"TS#{ts}#{event_id}",
    }


def device_pk(device_id: str) -> str:
    """Clave de partición del registro de dispositivos: ``DEVICE#{device_id}``."""
    return f"DEVICE#{validate_slug('device_id', device_id)}"


def validate_clip_key(clip_key: str, event: dict) -> None:
    """ACOTA ``clip_key`` a la identidad del evento (anti-spoof de media).

    Convención (CLAUDE.md §7):
    ``media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/{event_id}.{ext}``.
    Se exige el prefijo de identidad y que el nombre de archivo sea el
    ``event_id`` propio (un payload no puede enlazar un clip de otra cámara).
    """
    site_id = event["site_id"]
    device_id = event["device_id"]
    camera_id = event["camera_id"]
    event_id = event["event_id"]

    prefix = f"media/{site_id}/{device_id}/{camera_id}/"
    if not clip_key.startswith(prefix):
        raise ValueError(
            f"clip_key fuera de la identidad del evento: {clip_key!r} no empieza por {prefix!r}"
        )
    # Estructura YYYY/MM/DD/{event_id}.{ext} tras el prefijo de identidad.
    tail = clip_key[len(prefix) :]
    if re.fullmatch(rf"\d{{4}}/\d{{2}}/\d{{2}}/{re.escape(event_id)}\.[a-z0-9]+", tail) is None:
        raise ValueError(
            f"clip_key no respeta YYYY/MM/DD/{{event_id}}.{{ext}} para event_id {event_id!r}: "
            f"{clip_key!r}"
        )
