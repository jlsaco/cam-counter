"""``cam_counter_edge``: subsistema de conteo de personas en el borde.

Scaffold del producto de conteo edge-first (Raspberry Pi 5 + Hailo-8). Este
paquete expone el ``Detector`` (acoplado a Hailo, con import perezoso), el
``DummyDetector`` (determinista, sin hardware, para CI x86), los tipos
compartidos y la validación de identificadores.

El nombre es ``cam_counter_edge`` (con prefijo de proyecto) para evitar colisión
de namespace con ``v1/api`` y ``ota`` en el entorno de CI compartido.
"""

from __future__ import annotations

from .detector import CONF, HEF_PATH, PERSON_ID, Detector, parse_nms_class
from .dummy import DummyDetector, default_crossing_script
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
    BBOX_ORDER,
    PERSON_CLASS_ID,
    CrossingEvent,
    Detection,
    Line,
    LineConfig,
    Point,
    Track,
)

__all__ = [
    "BBOX_ORDER",
    "CONF",
    "HEF_PATH",
    "MAX_SLUG_LEN",
    "PERSON_CLASS_ID",
    "PERSON_ID",
    "SLUG_PATTERN",
    "CrossingEvent",
    "Detection",
    "Detector",
    "DummyDetector",
    "InvalidSlugError",
    "Line",
    "LineConfig",
    "Point",
    "Track",
    "default_crossing_script",
    "is_valid_slug",
    "make_camera_id",
    "parse_nms_class",
    "validate_camera_id",
    "validate_device_id",
    "validate_site_id",
    "validate_slug",
]
