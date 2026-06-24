"""``cam_counter_edge``: subsistema de conteo de personas en el borde.

Scaffold del producto de conteo edge-first (Raspberry Pi 5 + Hailo-8). Este
paquete expone el ``Detector`` (acoplado a Hailo, con import perezoso), el
``DummyDetector`` (determinista, sin hardware, para CI x86), los tipos
compartidos y la validación de identificadores.

El nombre es ``cam_counter_edge`` (con prefijo de proyecto) para evitar colisión
de namespace con ``v1/api`` y ``ota`` en el entorno de CI compartido.
"""

from __future__ import annotations

from .clip import ClipEncodeError, ClipRecorder, ClipResult, write_clip
from .command_handler import CommandError, CommandHandler
from .config import ConfigWatcher
from .detector import CONF, HEF_PATH, PERSON_ID, Detector, parse_nms_class
from .dummy import DummyDetector, default_crossing_script, smooth_crossing_script
from .identifiers import (
    MAX_SLUG_LEN,
    MEDIA_BUCKET,
    SLUG_PATTERN,
    InvalidSlugError,
    is_valid_slug,
    make_camera_id,
    media_clip_key,
    validate_camera_id,
    validate_device_id,
    validate_site_id,
    validate_slug,
)
from .line_config_contract import (
    LineConfigContractError,
    line_config_from_document,
    line_config_to_document,
    load_line_config_schema,
)
from .line_counter import LineCounter, compute_event_id, ms_to_iso_utc, signed_side
from .shadow_reconciler import (
    ReconcileOutcome,
    ShadowReconciler,
    command_request_topic,
    line_config_shadow_name,
    named_shadow_topic,
)
from .store import SCHEMA_USER_VERSION, StaleConfigVersionError, Store
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
    "MEDIA_BUCKET",
    "PERSON_CLASS_ID",
    "PERSON_ID",
    "SCHEMA_USER_VERSION",
    "SLUG_PATTERN",
    "ClipEncodeError",
    "ClipRecorder",
    "ClipResult",
    "CommandError",
    "CommandHandler",
    "ConfigWatcher",
    "CrossingEvent",
    "Detection",
    "Detector",
    "DummyDetector",
    "InvalidSlugError",
    "Line",
    "LineConfig",
    "LineConfigContractError",
    "LineCounter",
    "Point",
    "ReconcileOutcome",
    "ShadowReconciler",
    "StaleConfigVersionError",
    "Store",
    "Track",
    "command_request_topic",
    "compute_event_id",
    "default_crossing_script",
    "is_valid_slug",
    "line_config_from_document",
    "line_config_shadow_name",
    "line_config_to_document",
    "load_line_config_schema",
    "make_camera_id",
    "media_clip_key",
    "ms_to_iso_utc",
    "named_shadow_topic",
    "parse_nms_class",
    "signed_side",
    "smooth_crossing_script",
    "validate_camera_id",
    "validate_device_id",
    "validate_site_id",
    "validate_slug",
    "write_clip",
]
