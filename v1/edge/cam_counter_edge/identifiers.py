"""Validación de slugs de identificadores y construcción de ``camera_id``.

``site_id`` / ``device_id`` / ``camera_id`` son slugs ASCII en minúscula que
cumplen el regex ``^[a-z0-9][a-z0-9-]{1,62}$``:

- empiezan por ``[a-z0-9]`` y siguen con ``[a-z0-9-]``,
- longitud total 2..63 (mínimo 2 por el regex; cadena vacía RECHAZADA),
- PROHIBIDOS ``#`` (delimita claves compuestas de DynamoDB), ``/`` (delimita
  rutas/keys de S3), mayúsculas y longitud > 63.

La validación del regex se aplica **ANTES** de construir cualquier clave de
DynamoDB o S3.

REGLA DURA: ``device_id`` y ``camera_id`` se almacenan y pasan como CAMPOS
SEPARADOS explícitos. **NUNCA** se reconstruye ``device_id`` a partir de un
split de ``camera_id`` (aunque ``camera_id`` tenga la forma
``{device_id}-cam{N}``): un ``device_id`` puede contener ``-`` y el split sería
ambiguo. ``make_camera_id`` *construye* el id compuesto, pero el inverso por
split está deliberadamente PROHIBIDO y no se ofrece.
"""

from __future__ import annotations

import re

# Patrón canónico de slug (idéntico al de contracts/*.schema.json).
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"
SLUG_RE = re.compile(SLUG_PATTERN)

# Longitud máxima permitida (coherente con el cuantificador {1,62} del regex).
MAX_SLUG_LEN = 63


class InvalidSlugError(ValueError):
    """Se lanza cuando un identificador no cumple el regex de slug."""


def is_valid_slug(value: object) -> bool:
    """Devuelve ``True`` si ``value`` es un slug válido (str que casa el regex).

    Rechaza no-strings, cadena vacía, mayúsculas, ``#``, ``/`` y longitud > 63.
    """
    return isinstance(value, str) and SLUG_RE.match(value) is not None


def validate_slug(value: object, kind: str = "id") -> str:
    """Valida ``value`` como slug y lo devuelve; lanza ``InvalidSlugError`` si no.

    Args:
        value: candidato a slug.
        kind: nombre del identificador (para el mensaje de error), p.ej.
            ``"site_id"``.
    """
    if not is_valid_slug(value):
        raise InvalidSlugError(
            f"{kind} inválido: {value!r}. Debe cumplir {SLUG_PATTERN} "
            "(ASCII minúscula, 2..63 chars, sin '#', sin '/', sin mayúsculas)."
        )
    assert isinstance(value, str)  # garantizado por is_valid_slug
    return value


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
    """Construye el ``camera_id`` global único ``{device_id}-cam{N}``.

    Valida el ``device_id`` de entrada y el ``camera_id`` resultante. ``n`` debe
    ser un entero >= 0.

    NOTA: esta función construye el id compuesto, pero ``device_id`` y
    ``camera_id`` se almacenan SEPARADOS; el ``device_id`` nunca se recupera por
    split de ``camera_id``.
    """
    validate_device_id(device_id)
    if not isinstance(n, int) or isinstance(n, bool) or n < 0:
        raise InvalidSlugError(f"índice de cámara inválido: {n!r}; debe ser int >= 0.")
    camera_id = f"{device_id}-cam{n}"
    return validate_camera_id(camera_id)
