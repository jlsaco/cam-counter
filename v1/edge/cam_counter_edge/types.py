"""Tipos compartidos del subsistema de conteo en el borde.

Toda la geometría usa **floats normalizados 0..1** relativos al frame ORIGINAL de
inferencia, con origen arriba-izquierda (nunca píxeles; ver CLAUDE.md §4). El orden de
bounding-box de TODO el sistema es ``[xmin, ymin, xmax, ymax]``.

Estos tipos son el espejo en runtime de los contratos JSON Schema de ``contracts/``
(``crossing_event.schema.json`` y ``line_config.schema.json``). Los nombres de campo
canónicos viven en esos contratos; aquí se exponen como constantes/listas para que el
resto del paquete se alinee sin re-teclear los nombres y sin hacer drift de esquema.
"""

from __future__ import annotations

from dataclasses import dataclass, field

# Punto normalizado 0..1 ``(x, y)`` con origen arriba-izquierda. Tanto los extremos A,B de
# la línea-umbral como los centroides de track usan esta forma.
Point = tuple[float, float]

# clase 0 = persona en la salida NMS-por-clase del modelo YOLOv8s sobre COCO.
PERSON_CLASS_ID = 0
# Umbral de confianza por defecto (idéntico al pipeline histórico CONF=0.45).
DEFAULT_CONF = 0.45

# ───────────────────────── Alineación con contracts/ ──────────────────────────
# Versión de esquema de ambos contratos (schema_version = 1).
SCHEMA_VERSION = 1

# Nombres EXACTOS de campo del contrato CrossingEvent (snake_case). Se mantienen como
# tupla para que SQLite/DynamoDB (PRs posteriores) y los validadores se alineen sin
# re-teclear los nombres. Fuente de verdad: contracts/crossing_event.schema.json.
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

# Valores de cable/almacenados del sentido del cruce (los términos humanos
# 'subieron'/'bajaron' los aportan positive_label/negative_label, no este campo).
CROSSING_DIRECTIONS = ("in", "out")
# Estados del ciclo de vida del clip de media.
CLIP_STATUSES = ("pending", "uploading", "uploaded", "failed")

# Nombres EXACTOS de campo del contrato LineConfig. Fuente de verdad:
# contracts/line_config.schema.json. OJO: la versión monótona de la línea se llama
# ``config_version`` en LineConfig; en CrossingEvent su espejo se llama ``line_version``.
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


def _clamp01(value: float) -> float:
    """Recorta un escalar al rango cerrado [0.0, 1.0] (invariante de coordenadas)."""
    if value < 0.0:
        return 0.0
    if value > 1.0:
        return 1.0
    return value


@dataclass
class Detection:
    """Una detección de persona en un frame, en coordenadas normalizadas 0..1.

    Attributes:
        bbox_norm: ``[xmin, ymin, xmax, ymax]`` (ORDEN DEL SISTEMA), floats 0..1.
        class_id: id de clase COCO; 0 = persona.
        confidence: score de la detección en 0..1.
    """

    bbox_norm: list[float]
    class_id: int = PERSON_CLASS_ID
    confidence: float = 0.0

    @property
    def center(self) -> tuple[float, float]:
        """Centroide ``(cx, cy)`` normalizado 0..1 del bounding-box."""
        xmin, ymin, xmax, ymax = self.bbox_norm
        return ((xmin + xmax) / 2.0, (ymin + ymax) / 2.0)


@dataclass
class Track:
    """Track estable de una persona a través de frames (campos mínimos para PR06).

    El tracker real (asociación, histéresis, conteo de cruce) llega en PR06; aquí solo
    se fija la forma mínima razonable: un id estable más el último bbox/centroide.

    Attributes:
        track_id: identificador estable del track; único dentro de la cámara durante
            la vida del track (se persiste como string en el contrato CrossingEvent).
        bbox_norm: último ``[xmin, ymin, xmax, ymax]`` observado, normalizado 0..1.
        centroid: último centroide ``(cx, cy)`` normalizado 0..1.
        confidence: última confianza observada.
        class_id: id de clase (0 = persona).
        hits: nº de detecciones asociadas a este track (señal de madurez).
        last_seen_ms: epoch ms UTC de la última actualización, o None si desconocido.
    """

    track_id: str
    bbox_norm: list[float]
    centroid: tuple[float, float]
    confidence: float = 0.0
    class_id: int = PERSON_CLASS_ID
    hits: int = 1
    last_seen_ms: int | None = None


def parse_nms_class(
    class_dets,
    conf: float = DEFAULT_CONF,
    class_id: int = PERSON_CLASS_ID,
) -> list[Detection]:
    """Parsea la salida NMS-por-clase de UNA clase a una lista de ``Detection``.

    Función **PURA**: sin Hailo, sin OpenCV, sin red, sin I/O. Es la lógica extraída del
    ``infer_loop`` histórico (``v1/detection/yolo_personas_mt.py``), aislada para poder
    testearla en x86 sin hardware.

    La salida en chip del Hailo es ``HAILO NMS BY CLASS``: para una clase, cada caja
    llega como ``[ymin, xmin, ymax, xmax, score]`` **normalizada 0..1**. Esta función:

    1. filtra por umbral de confianza (``score >= conf``; idéntico a ``if sc < CONF:
       continue`` del código histórico, con CONF=0.45),
    2. **reordena** cada caja del orden Hailo ``[ymin, xmin, ymax, xmax]`` al orden del
       sistema ``[xmin, ymin, xmax, ymax]``,
    3. recorta defensivamente cada coordenada a 0..1.

    Args:
        class_dets: iterable de filas; cada fila es indexable ``row[0..4]`` =
            ``[ymin, xmin, ymax, xmax, score]``. Acepta ``numpy.ndarray`` o listas.
        conf: umbral de confianza (por defecto 0.45).
        class_id: id de clase a etiquetar en cada ``Detection`` (0 = persona).

    Returns:
        Lista de ``Detection`` con ``bbox_norm = [xmin, ymin, xmax, ymax]`` en 0..1.
    """
    detections: list[Detection] = []
    if class_dets is None:
        return detections
    for row in class_dets:
        ymin = float(row[0])
        xmin = float(row[1])
        ymax = float(row[2])
        xmax = float(row[3])
        score = float(row[4])
        if score < conf:
            continue
        bbox_norm = [_clamp01(xmin), _clamp01(ymin), _clamp01(xmax), _clamp01(ymax)]
        detections.append(
            Detection(bbox_norm=bbox_norm, class_id=class_id, confidence=score)
        )
    return detections


@dataclass
class LineConfig:
    """Configuración de la línea-umbral de conteo de UNA cámara (espejo de LineConfig).

    Fuente de verdad del esquema: ``contracts/line_config.schema.json``. La geometría es
    NORMALIZADA 0..1 (nunca píxeles). ``positive_side`` (+1/-1) selecciona qué semiplano
    cuenta como ``'in'`` y, por tanto, qué flip de signo mapea a cada ``direction``.

    Attributes:
        site_id/device_id/camera_id: slugs (se validan en el LineCounter antes de usarlos).
        a, b: extremos ``(x, y)`` normalizados del segmento de la línea-umbral.
        positive_side: +1 o -1; signo del producto cruzado que cuenta como ``'in'``.
        positive_label/negative_label: etiquetas humanas de cada sentido (p.ej.
            ``'subieron'``/``'bajaron'``); son SÓLO presentación.
        config_version: versión monótona de la config (espejo de ``line_version`` en el
            CrossingEvent que se emita bajo esta config).
    """

    site_id: str
    device_id: str
    camera_id: str
    a: Point
    b: Point
    positive_side: int = 1
    positive_label: str = "in"
    negative_label: str = "out"
    config_version: int = 1


@dataclass
class CrossingEvent:
    """Evento de cruce de la línea-umbral (espejo runtime del contrato CrossingEvent).

    Fuente de verdad del esquema: ``contracts/crossing_event.schema.json`` (snake_case,
    ``schema_version = 1``). ``event_id`` es DETERMINISTA =
    ``sha1('{site}|{device}|{camera}|{track}|{crossing_seq}')`` en hex minúscula, lo que hace
    IDEMPOTENTE el sync a la nube (un reintento del mismo ``event_id`` no duplica).

    ``positive_label``/``negative_label`` se llevan en memoria como ayuda de presentación,
    pero NO son columnas persistidas: el store materializa la etiqueta ya resuelta en
    ``label`` (más ``direction``). Los campos de clip nacen como ``clip_key=None`` /
    ``clip_status='pending'`` (el recorder de clips llega en un PR posterior).
    """

    event_id: str
    site_id: str
    device_id: str
    camera_id: str
    track_id: str
    crossing_seq: int
    direction: str
    label: str
    line_version: int
    ts_event_ms: int
    ts_event_iso: str
    confidence: float = 0.0
    clip_key: str | None = None
    clip_status: str = "pending"
    schema_version: int = SCHEMA_VERSION
    synced: int = 0
    created_at: str = ""
    # Presentación únicamente (no se persisten como columnas; ver docstring).
    positive_label: str = field(default="in")
    negative_label: str = field(default="out")
