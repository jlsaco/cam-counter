"""``cam_counter_edge``: subsistema de conteo de personas en el borde.

Scaffold del producto de conteo edge-first (Raspberry Pi 5 + Hailo-8). Este
paquete expone el ``Detector`` (acoplado a Hailo, con import perezoso), el
``DummyDetector`` (determinista, sin hardware, para CI x86), los tipos
compartidos y la validación de identificadores.

El nombre es ``cam_counter_edge`` (con prefijo de proyecto) para evitar colisión
de namespace con ``v1/api`` y ``ota`` en el entorno de CI compartido.
"""

from __future__ import annotations

from .app import (
    CameraHealth,
    CameraPipeline,
    CameraSpec,
    Supervisor,
    build_dummy_supervisor,
)
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
from .line_counter import LineCounter, compute_event_id, ms_to_iso_utc, signed_side
from .store import SCHEMA_USER_VERSION, Store
from .sync import (
    AwsClients,
    CloudSyncWorker,
    DrainResult,
    SyncConfig,
    SyncOfflineError,
    build_boto3_clients,
    media_key_for,
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
    "SCHEMA_USER_VERSION",
    "SLUG_PATTERN",
    "AwsClients",
    "CameraHealth",
    "CameraPipeline",
    "CameraSpec",
    "CloudSyncWorker",
    "CrossingEvent",
    "Detection",
    "Detector",
    "DrainResult",
    "DummyDetector",
    "InvalidSlugError",
    "Line",
    "LineConfig",
    "LineCounter",
    "Point",
    "Store",
    "Supervisor",
    "SyncConfig",
    "SyncOfflineError",
    "Track",
    "build_boto3_clients",
    "build_dummy_supervisor",
    "compute_event_id",
    "default_crossing_script",
    "is_valid_slug",
    "make_camera_id",
    "media_key_for",
    "ms_to_iso_utc",
    "parse_nms_class",
    "signed_side",
    "validate_camera_id",
    "validate_device_id",
    "validate_site_id",
    "validate_slug",
]
