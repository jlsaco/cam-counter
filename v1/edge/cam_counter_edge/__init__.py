"""``cam_counter_edge``: subsistema de conteo de personas en el borde (edge-first).

Paquete instalable con nombre **prefijado por proyecto** (``cam_counter_edge``, NO el
genérico ``counter``) para evitar colisión de namespace en el CI compartido del monorepo.

Este PR aporta solo el **scaffold**: ``Detector`` (wrapper Hailo con import perezoso y
``VDevice`` inyectable), ``DummyDetector`` (misma interfaz, secuencia determinista),
tipos compartidos (``Detection``/``Track``) alineados con ``contracts/``, y validación de
slugs en ``identifiers``. El tracker, el conteo de cruce, SQLite, clips, API y OTA llegan
en PRs posteriores.

Importar este paquete en x86 SIN ``hailo_platform`` funciona: el acoplamiento a Hailo está
aislado en ``Detector`` con import perezoso.
"""

from __future__ import annotations

from .detector import DEFAULT_HEF_PATH, Detector
from .dummy import DummyDetector
from .identifiers import (
    MAX_SLUG_LEN,
    SLUG_PATTERN,
    InvalidSlugError,
    is_valid_slug,
    make_camera_id,
    validate_camera_id,
    validate_device_id,
    validate_site_id,
    validate_slug,
)
from .types import (
    CLIP_STATUSES,
    CROSSING_DIRECTIONS,
    CROSSING_EVENT_FIELDS,
    DEFAULT_CONF,
    LINE_CONFIG_FIELDS,
    PERSON_CLASS_ID,
    SCHEMA_VERSION,
    Detection,
    Track,
    parse_nms_class,
)

__all__ = [
    # detector
    "Detector",
    "DEFAULT_HEF_PATH",
    # dummy
    "DummyDetector",
    # types
    "Detection",
    "Track",
    "parse_nms_class",
    "PERSON_CLASS_ID",
    "DEFAULT_CONF",
    "SCHEMA_VERSION",
    "CROSSING_EVENT_FIELDS",
    "CROSSING_DIRECTIONS",
    "CLIP_STATUSES",
    "LINE_CONFIG_FIELDS",
    # identifiers
    "SLUG_PATTERN",
    "MAX_SLUG_LEN",
    "InvalidSlugError",
    "is_valid_slug",
    "validate_slug",
    "validate_site_id",
    "validate_device_id",
    "validate_camera_id",
    "make_camera_id",
]
