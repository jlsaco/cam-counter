"""Validación de slugs de identificadores y construcción de ``camera_id``.

Convención transversal (CLAUDE.md §3): ``site_id``, ``device_id`` y ``camera_id`` son
slugs ASCII en minúsculas que cumplen el regex ``^[a-z0-9][a-z0-9-]{1,62}$`` (longitud
2..63). Se PROHÍBEN ``#`` (delimita claves compuestas PK/SK de DynamoDB) y ``/`` (delimita
rutas/keys de S3), así como mayúsculas y la cadena vacía. La validación se aplica **ANTES**
de construir cualquier clave de S3 o DynamoDB.

IMPORTANTE: ``device_id`` y ``camera_id`` se almacenan y se pasan como **campos SEPARADOS**
y explícitos. **NUNCA** se reconstruye ``device_id`` a partir de un split de ``camera_id``:
aunque ``camera_id`` tenga la forma ``'{device_id}-cam{N}'``, partirlo es frágil (un
``device_id`` puede contener ``-``) y está prohibido. Cada uno se valida y se persiste por
su cuenta.
"""

from __future__ import annotations

import re
from datetime import datetime, timezone

# Regex canónico de slug (longitud efectiva 2..63: 1 char inicial + {1,62}).
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
_SLUG_RE = re.compile(SLUG_PATTERN)
# Longitud máxima permitida de un slug (cota dura).
MAX_SLUG_LEN = 63

# Bucket de MEDIA del producto (clips/gifs/snapshots). Es la regla de los TRES buckets de
# CLAUDE.md §7: NUNCA se confunde con el de artefactos rpi ni con el de releases. Aquí sólo
# se usa el NOMBRE/plantilla de clave para planificar la subida (PR10 hace la subida real);
# este PR NO accede a S3. No es un secreto: es un identificador público de recurso.
MEDIA_BUCKET = "cam-counter-media-950639281773"

# event_id determinista del contrato CrossingEvent = sha1 hex minúscula (40 chars). El sha1
# es NO CRIPTOGRÁFICO (sólo dedupe); ver line_counter.make_event_id. Se valida su forma
# ANTES de incrustarlo en una clave S3 (no debe contener '/' ni '#').
_EVENT_ID_RE = re.compile(r"^[0-9a-f]{40}$")
# Extensión de media admitida (la real escrita por el grabador: mp4 o gif por ahora).
_MEDIA_EXT_RE = re.compile(r"^[a-z0-9]{2,4}$")


class InvalidSlugError(ValueError):
    """Se lanza cuando un identificador no cumple el regex de slug canónico."""


def is_valid_slug(value: object) -> bool:
    """Devuelve True si ``value`` es un slug válido (``^[a-z0-9][a-z0-9-]{1,62}$``).

    Rechaza (devuelve False) ante: no-string, cadena vacía, mayúsculas, ``#``, ``/``, y
    longitud > 63. No lanza; es el predicado puro.
    """
    if not isinstance(value, str):
        return False
    if len(value) > MAX_SLUG_LEN:
        return False
    return _SLUG_RE.match(value) is not None


def validate_slug(value: object, kind: str = "id") -> str:
    """Valida un slug y lo devuelve; lanza ``InvalidSlugError`` si no cumple.

    Args:
        value: candidato a slug.
        kind: nombre del campo (p.ej. ``"site_id"``) para un mensaje de error claro.
    """
    if not is_valid_slug(value):
        raise InvalidSlugError(
            f"{kind} inválido: {value!r} no cumple {SLUG_PATTERN} "
            f"(ASCII minúscula, sin '#' ni '/', sin mayúsculas, no vacío, longitud 2..{MAX_SLUG_LEN})"
        )
    # is_valid_slug ya garantizó que es str.
    return value  # type: ignore[return-value]


def validate_site_id(site_id: object) -> str:
    """Valida un ``site_id`` y lo devuelve."""
    return validate_slug(site_id, "site_id")


def validate_device_id(device_id: object) -> str:
    """Valida un ``device_id`` y lo devuelve."""
    return validate_slug(device_id, "device_id")


def validate_camera_id(camera_id: object) -> str:
    """Valida un ``camera_id`` y lo devuelve."""
    return validate_slug(camera_id, "camera_id")


def make_camera_id(device_id: str, n: int) -> str:
    """Construye y valida ``camera_id = '{device_id}-cam{N}'``.

    Valida AMBOS campos por separado: primero ``device_id`` y luego el ``camera_id``
    resultante (que podría exceder los 63 chars si ``device_id`` es muy largo, en cuyo
    caso se rechaza). ``device_id`` y ``camera_id`` quedan como campos separados; este
    helper NO implica que ``device_id`` deba reconstruirse luego por split.

    Args:
        device_id: slug del dispositivo (Pi), ya en minúsculas.
        n: índice de cámara dentro del dispositivo (entero >= 0).

    Returns:
        El ``camera_id`` global único validado.
    """
    validate_device_id(device_id)
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise InvalidSlugError(f"índice de cámara inválido: {n!r} (se espera int >= 0)")
    camera_id = f"{device_id}-cam{n}"
    return validate_camera_id(camera_id)


def _validate_event_id_for_key(event_id: object) -> str:
    """Valida la forma del ``event_id`` ANTES de incrustarlo en una clave S3.

    Acepta sólo el sha1 hex de 40 chars del contrato (NO criptográfico; sólo dedupe). Esto
    garantiza, además del regex de slugs de los demás segmentos, que la clave nunca contenga
    ``/`` ni ``#`` (los delimitadores reservados de S3/DynamoDB).
    """
    if not isinstance(event_id, str) or _EVENT_ID_RE.match(event_id) is None:
        raise InvalidSlugError(
            f"event_id inválido: {event_id!r} (se espera sha1 hex de 40 chars, "
            "minúscula, sin '/' ni '#')"
        )
    return event_id


def _validate_media_ext(ext: object) -> str:
    """Valida la extensión de media (sin punto); minúscula alfanumérica corta."""
    if not isinstance(ext, str):
        raise InvalidSlugError(f"extensión de media inválida: {ext!r} (se espera str)")
    ext = ext.lower().lstrip(".")
    if _MEDIA_EXT_RE.match(ext) is None:
        raise InvalidSlugError(
            f"extensión de media inválida: {ext!r} (se espera 2..4 chars [a-z0-9])"
        )
    return ext


def build_media_key(
    site_id: object,
    device_id: object,
    camera_id: object,
    ts_event_ms: int,
    event_id: object,
    ext: object,
) -> str:
    """Construye la clave S3 de MEDIA del evento, VALIDANDO los slugs ANTES.

    Plantilla (CLAUDE.md §7): ``media/{site_id}/{device_id}/{camera_id}/{yyyy}/{mm}/{dd}/
    {event_id}.{ext}``. La fecha ``yyyy/mm/dd`` se deriva de ``ts_event_ms`` en **UTC**. La
    validación regex de cada slug (``site_id``/``device_id``/``camera_id``) y de la forma del
    ``event_id``/``ext`` se aplica ANTES de concatenar: un identificador inválido (con ``#``,
    ``/``, mayúsculas, vacío o > 63 chars) lanza ``InvalidSlugError`` y NUNCA llega a la
    clave. Este builder NO accede a S3 (sólo planifica la clave; la subida la hace PR10).

    Args:
        site_id/device_id/camera_id: slugs ASCII (se validan con el regex canónico).
        ts_event_ms: epoch ms UTC del evento; fija la partición ``yyyy/mm/dd``.
        event_id: sha1 hex (40 chars) determinista del CrossingEvent.
        ext: extensión sin punto del fichero realmente escrito (``'mp4'`` o ``'gif'``).

    Returns:
        La clave (key) S3 relativa al bucket :data:`MEDIA_BUCKET`.
    """
    site = validate_site_id(site_id)
    device = validate_device_id(device_id)
    camera = validate_camera_id(camera_id)
    eid = _validate_event_id_for_key(event_id)
    safe_ext = _validate_media_ext(ext)
    dt = datetime.fromtimestamp(int(ts_event_ms) / 1000.0, tz=timezone.utc)
    return (
        f"media/{site}/{device}/{camera}/"
        f"{dt.year:04d}/{dt.month:02d}/{dt.day:02d}/{eid}.{safe_ext}"
    )
