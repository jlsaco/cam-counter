"""Modelos Pydantic v2 canónicos de la API local.

Estos modelos son el ESPEJO EJECUTABLE de los JSON Schema de ``contracts/``:

- ``CrossingEvent``  <-> ``contracts/crossing_event.schema.json``
- ``LineConfig``     <-> ``contracts/line_config.schema.json``

El conjunto de NOMBRES DE CAMPO de cada modelo coincide EXACTAMENTE con las
propiedades del schema correspondiente (lo verifica
``tests/test_schemas_contracts.py``); cualquier rename es BREAKING y exige bump de
``schema_version`` + PR coordinado.

``Camera``, ``DeviceInfo``, ``Counters``, ``WsEnvelope`` y los modelos de salud no
tienen (todavía) un JSON Schema en ``contracts/``: son contratos de la capa
API↔UI y se definen aquí UNA sola vez. Se documenta el supuesto en el PR.

Coordenadas: SIEMPRE floats normalizados 0..1 relativos al frame original de
inferencia, origen arriba-izquierda; NUNCA píxeles.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field

# Patrón de slug idéntico al de ``contracts/*.schema.json`` y a
# ``cam_counter_edge.SLUG_PATTERN``.
SLUG_PATTERN = r"^[a-z0-9][a-z0-9-]{1,62}$"

Slug = Annotated[str, Field(pattern=SLUG_PATTERN)]
NormCoord = Annotated[float, Field(ge=0.0, le=1.0)]

Direction = Literal["in", "out"]
ClipStatus = Literal["pending", "uploading", "uploaded", "failed"]
PositiveSide = Literal[-1, 1]


class _Strict(BaseModel):
    """Base estricta: rechaza campos extra (refleja ``additionalProperties:false``)."""

    model_config = ConfigDict(extra="forbid")


# --------------------------------------------------------------------------- #
# Contratos espejo de contracts/ (NO renombrar campos sin bump de schema)
# --------------------------------------------------------------------------- #


class CrossingEvent(_Strict):
    """Evento de cruce de línea — espejo de ``contracts/crossing_event.schema.json``."""

    event_id: str = Field(pattern=r"^[0-9a-f]{40}$")
    site_id: Slug
    device_id: Slug
    camera_id: Slug
    track_id: str
    crossing_seq: int = Field(ge=0)
    direction: Direction
    positive_label: str | None = None
    negative_label: str | None = None
    label: str | None = None
    line_version: int | None = None
    ts_event_ms: int
    ts_event_iso: str
    confidence: float | None = None
    clip_key: str | None = None
    clip_status: ClipStatus | None = None
    schema_version: int = 1
    synced: int = 0
    created_at: str | None = None


class Point2D(_Strict):
    """Punto normalizado 0..1 (origen arriba-izquierda)."""

    x: NormCoord
    y: NormCoord


class LineGeom(_Strict):
    """Línea-umbral definida por dos endpoints normalizados ``a`` y ``b``."""

    a: Point2D
    b: Point2D


class LineConfig(_Strict):
    """Config de la línea por cámara — espejo de ``contracts/line_config.schema.json``."""

    site_id: Slug
    device_id: Slug
    camera_id: Slug
    config_version: int = Field(ge=0)
    line: LineGeom
    positive_side: PositiveSide
    positive_label: str | None = None
    negative_label: str | None = None
    updated_at: str | None = None
    schema_version: int = 1


# --------------------------------------------------------------------------- #
# Contratos de la capa API↔UI (sin JSON Schema en contracts/ todavía)
# --------------------------------------------------------------------------- #


class LineConfigUpdate(_Strict):
    """Cuerpo del ``PUT`` de config: geometría/labels/sentido + CAS de versión.

    ``site_id``/``device_id``/``camera_id`` NO los aporta el cliente: los resuelve
    el servidor desde la cámara (evita incoherencias). ``expected_config_version``
    es el ``config_version`` que el cliente cree vigente; si está desactualizado el
    PUT devuelve ``409`` (concurrencia optimista, compare-and-set).
    """

    line: LineGeom
    positive_side: PositiveSide
    positive_label: str | None = None
    negative_label: str | None = None
    expected_config_version: int = Field(ge=0)


class Camera(_Strict):
    """Descriptor de una cámara lógica del Pi."""

    camera_id: Slug
    site_id: Slug
    device_id: Slug
    config_version: int = Field(ge=0)
    has_config: bool
    frames_processed: int = Field(ge=0)
    online: bool


class DeviceInfo(_Strict):
    """Identidad y versión del dispositivo (``/api/device``)."""

    device_id: Slug
    site_id: Slug
    app_version: str
    git_sha: str
    camera_ids: list[Slug]
    db_schema_version: int
    fake_source: bool


class CounterDay(_Strict):
    """Conteo de una cámara para un día UTC y un sentido de cable."""

    day_utc: str
    direction: Direction
    count: int = Field(ge=0)


class Counters(_Strict):
    """Contadores agregados de una cámara (totales + desglose por día)."""

    camera_id: Slug
    in_count: int = Field(ge=0)
    out_count: int = Field(ge=0)
    net: int
    days: list[CounterDay]


class CameraHealth(_Strict):
    """Salud de PRODUCTO por cámara (criterio para el gate de OTA)."""

    camera_id: Slug
    frames_processed: int = Field(ge=0)
    last_inference_ts: int | None
    hailo_inference_ok: bool | None
    config_version: int = Field(ge=0)


class Health(_Strict):
    """Salud de PRODUCTO del proceso (no mera liveness).

    Un ``200`` con ``frames_processed=0`` en todas las cámaras es DISTINGUIBLE de
    salud real: ``frames_flowing`` es ``False`` cuando ninguna cámara ha procesado
    frames todavía.
    """

    status: Literal["ok", "degraded"]
    app_version: str
    db_schema_version: int
    fake_source: bool
    frames_flowing: bool
    cameras: list[CameraHealth]


WsType = Literal["counter_update", "camera_status", "config_changed", "crossing"]


class WsEnvelope(_Strict):
    """Sobre de los mensajes del hub WebSocket (``/api/ws``)."""

    type: WsType
    camera_id: Slug
    ts_ms: int
    data: dict[str, Any] = Field(default_factory=dict)
