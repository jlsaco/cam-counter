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

# Regex canónico de slug (longitud efectiva 2..63: 1 char inicial + {1,62}).
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
_SLUG_RE = re.compile(SLUG_PATTERN)
# Longitud máxima permitida de un slug (cota dura).
MAX_SLUG_LEN = 63


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
