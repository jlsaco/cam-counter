"""Tipos compartidos del subsistema de conteo en el borde.

Define las estructuras ligeras que circulan por el pipeline
(`captura -> detect -> track -> count`) y los modelos que reflejan los
contratos canónicos de ``contracts/`` (JSON Schema draft 2020-12):

- ``Detection``: salida del detector para un frame (una caja por persona).
- ``Track``: identidad estable de un objeto a lo largo de frames (lo usa PR06).
- ``CrossingEvent`` / ``LineConfig``: dataclasses cuyos NOMBRES DE CAMPO se
  alinean EXACTAMENTE con ``contracts/crossing_event.schema.json`` y
  ``contracts/line_config.schema.json``. Son el espejo ejecutable de esos
  schemas; cualquier rename es BREAKING y exige bump de ``schema_version``.

Convenciones transversales (ver CLAUDE.md):
- Geometría SIEMPRE en floats normalizados 0..1 relativos al frame original de
  inferencia, origen arriba-izquierda. NUNCA píxeles.
- ``bbox_norm`` usa el orden del SISTEMA ``[xmin, ymin, xmax, ymax]`` (el
  ``Detector`` reordena la salida de Hailo ``[ymin, xmin, ymax, xmax]``).
- ``site_id`` / ``device_id`` / ``camera_id`` son slugs separados; nunca se
  reconstruye uno a partir de otro (ver ``identifiers``).
"""

from __future__ import annotations

from dataclasses import dataclass

# Orden del sistema para todas las cajas normalizadas 0..1.
# (El Detector traduce el orden de Hailo [ymin, xmin, ymax, xmax] a este.)
BBOX_ORDER = ("xmin", "ymin", "xmax", "ymax")

# class_id COCO de "persona" (clase 0), confirmado en docs/HALLAZGOS.md.
PERSON_CLASS_ID = 0


@dataclass
class Detection:
    """Detección de una persona en un frame.

    Attributes:
        bbox_norm: caja ``[xmin, ymin, xmax, ymax]`` en floats normalizados 0..1.
        class_id: clase COCO (0 = persona).
        confidence: score de la detección en 0..1.
    """

    bbox_norm: list[float]
    class_id: int = PERSON_CLASS_ID
    confidence: float = 0.0

    @property
    def centroid(self) -> tuple[float, float]:
        """Centroide normalizado ``(cx, cy)`` de la caja."""
        xmin, ymin, xmax, ymax = self.bbox_norm
        return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)


@dataclass
class Track:
    """Identidad estable de un objeto a lo largo de frames.

    Campos mínimos razonables para el tracker de PR06; ``track_id`` es estable
    entre frames y se usa (como string) en ``CrossingEvent.track_id``.
    """

    track_id: str
    bbox_norm: list[float]
    class_id: int = PERSON_CLASS_ID
    confidence: float = 0.0
    hits: int = 1
    last_ts_ms: int | None = None

    @property
    def centroid(self) -> tuple[float, float]:
        """Centroide normalizado ``(cx, cy)`` del último bbox del track."""
        xmin, ymin, xmax, ymax = self.bbox_norm
        return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)


@dataclass
class Point:
    """Punto normalizado 0..1 (origen arriba-izquierda)."""

    x: float
    y: float


@dataclass
class Line:
    """Línea-umbral definida por dos endpoints normalizados ``a`` y ``b``."""

    a: Point
    b: Point


@dataclass
class CrossingEvent:
    """Evento de cruce de línea (snake_case, ``schema_version=1``).

    Espejo ejecutable de ``contracts/crossing_event.schema.json``: los nombres
    de campo coinciden EXACTAMENTE con las propiedades de ese JSON Schema. Este
    PR sólo define el tipo; la generación/persistencia llega en PRs posteriores.
    """

    # Campos requeridos por el schema (sin valor por defecto).
    event_id: str
    site_id: str
    device_id: str
    camera_id: str
    track_id: str
    crossing_seq: int
    direction: str
    ts_event_ms: int
    ts_event_iso: str
    # Campos opcionales del schema.
    positive_label: str | None = None
    negative_label: str | None = None
    label: str | None = None
    line_version: int | None = None
    confidence: float | None = None
    clip_key: str | None = None
    clip_status: str | None = None
    synced: int = 0
    created_at: str | None = None
    # Requerido por el schema pero con const=1.
    schema_version: int = 1


@dataclass
class LineConfig:
    """Config de la línea-umbral por cámara (hot-reload vía ``config_version``).

    Espejo ejecutable de ``contracts/line_config.schema.json``. NOTA: el campo
    monótono de versión se llama ``config_version`` (no ``line_version``); se
    refleja en ``CrossingEvent.line_version`` al contar un cruce.
    """

    site_id: str
    device_id: str
    camera_id: str
    config_version: int
    line: Line
    positive_side: int
    positive_label: str | None = None
    negative_label: str | None = None
    updated_at: str | None = None
    schema_version: int = 1


# Listas de nombres de campo, útiles para verificación contra contracts/.
CROSSING_EVENT_FIELDS = (
    "event_id",
    "site_id",
    "device_id",
    "camera_id",
    "track_id",
    "crossing_seq",
    "direction",
    "positive_label",
    "negative_label",
    "label",
    "line_version",
    "ts_event_ms",
    "ts_event_iso",
    "confidence",
    "clip_key",
    "clip_status",
    "schema_version",
    "synced",
    "created_at",
)

LINE_CONFIG_FIELDS = (
    "site_id",
    "device_id",
    "camera_id",
    "config_version",
    "line",
    "positive_side",
    "positive_label",
    "negative_label",
    "updated_at",
    "schema_version",
)
