"""Etapa ``count`` del pipeline de borde: ``LineCounter`` por cámara.

Detecta el CRUCE de una línea-umbral configurable por dos extremos normalizados
``A`` y ``B``, con HISTÉRESIS (banda muerta anti-jitter) e IDEMPOTENCIA por track
(un mismo cruce no se recuenta). Produce ``CrossingEvent`` con ``event_id``
DETERMINISTA para que la sincronización edge->cloud sea idempotente.

Convención de signo (el contrato compartido MÁS frágil — ver CLAUDE.md):

    side(P) = sign(cross(B - A, P - A)),   cross(u, v) = u.x*v.y - u.y*v.x

Un cruce ocurre cuando el signo de ``side`` cambia (flip) entre centroides
consecutivos del MISMO track. El flag por cámara ``positive_side`` (+1/-1)
selecciona qué flip cuenta como ``direction='in'``: el cruce HACIA el semiplano
``positive_side`` es ``'in'``; el contrario es ``'out'``. ``'in'``/``'out'`` es
el único valor de cable/almacenado; ``positive_label``/``negative_label`` son las
etiquetas humanas (p.ej. ``'subieron'``/``'bajaron'``).

Robustez:

- **Histéresis ``min_frames``**: el track debe permanecer ``min_frames`` frames
  consecutivos en el NUEVO semiplano antes de confirmar el cruce. Un centroide
  que tiembla SOBRE la línea (jitter) no acumula frames consecutivos y por tanto
  NO produce doble conteo.
- **Un evento por cruce**: tras confirmar, el semiplano estable pasa a ser el
  nuevo; no se vuelve a emitir hasta que haya un flip genuinamente nuevo. Un
  ``cooldown`` opcional añade una banda muerta temporal extra.

Idempotencia del ``event_id``: cada cruce confirmado toma un ``crossing_seq``
MONÓTONO POR CÁMARA de ``store.next_crossing_seq``. Así, aunque el ``track_id`` se
reutilice (un track se retira y otro reaparece con el mismo id), dos cruces
distintos obtienen ``crossing_seq`` distintos y por tanto ``event_id`` distintos.

Este módulo NO escribe el evento en la DB (sólo pide ``next_crossing_seq``): la
persistencia la decide el caller llamando a ``store.record_event(event)`` (insert
idempotente + bump de contador). El contrato es deliberadamente: *``process``
genera eventos; el caller los persiste*.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Protocol

from .identifiers import validate_camera_id, validate_device_id, validate_site_id
from .types import CrossingEvent, LineConfig, Point

__all__ = [
    "LineCounter",
    "compute_event_id",
    "ms_to_iso_utc",
    "signed_side",
]

# Tipo de un punto/centroide normalizado 0..1 como par ``(x, y)``.
XY = tuple[float, float]


def _as_xy(p: object) -> XY:
    """Normaliza un extremo/centroide a ``(x, y)`` (acepta ``Point`` o par)."""
    if isinstance(p, Point):
        return (float(p.x), float(p.y))
    x, y = p  # type: ignore[misc]  # par (x, y) o secuencia de 2 floats
    return (float(x), float(y))


def signed_side(a: object, b: object, p: object) -> int:
    """Devuelve ``sign(cross(B - A, P - A))`` ∈ ``{-1, 0, +1}`` (geometría pura).

    ``cross(u, v) = u.x*v.y - u.y*v.x``. Es ``0`` sólo cuando ``P`` cae EXACTAMENTE
    sobre la recta A-B. Función pura y aislada para poder asertar el signo directo
    (es la convención compartida más frágil del sistema).
    """
    ax, ay = _as_xy(a)
    bx, by = _as_xy(b)
    px, py = _as_xy(p)
    cross = (bx - ax) * (py - ay) - (by - ay) * (px - ax)
    if cross > 0.0:
        return 1
    if cross < 0.0:
        return -1
    return 0


def ms_to_iso_utc(ts_event_ms: int) -> str:
    """ISO-8601 UTC determinista (precisión de ms) derivado del epoch ms."""
    dt = datetime.fromtimestamp(ts_event_ms / 1000.0, tz=UTC)
    return dt.isoformat(timespec="milliseconds").replace("+00:00", "Z")


def compute_event_id(
    site_id: str, device_id: str, camera_id: str, track_id: str, crossing_seq: int
) -> str:
    """``event_id`` DETERMINISTA = sha1 hex-minúscula de la tupla de identidad.

    Fórmula: ``sha1(f"{site_id}|{device_id}|{camera_id}|{track_id}|{crossing_seq}")``.
    El sha1 aquí NO es criptográfico: se usa SÓLO para deduplicación idempotente
    del sync edge->cloud (un reintento del mismo ``event_id`` no duplica). No
    protege ningún secreto; por eso es seguro frente a gitleaks/security-review.
    """
    raw = f"{site_id}|{device_id}|{camera_id}|{track_id}|{crossing_seq}"
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()  # noqa: S324 (dedupe, no crypto)


class _SeqSource(Protocol):
    """Interfaz mínima que ``LineCounter`` necesita del ``store`` (sólo el seq)."""

    def next_crossing_seq(self, camera_id: str) -> int: ...


class _TrackLike(Protocol):
    """Forma mínima de un track vivo: ``track_id`` + ``centroid`` normalizado."""

    track_id: object
    centroid: XY


@dataclass
class _TrackState:
    """Estado de histéresis por track (máquina de estado del cruce).

    Attributes:
        stable_side: semiplano CONFIRMADO del track (±1), o ``None`` hasta la
            primera observación con signo no nulo.
        pending_side: semiplano candidato (distinto del estable) que el track ha
            empezado a ocupar, o ``None``.
        pending_count: frames consecutivos en ``pending_side``.
        cooldown_remaining: frames de banda muerta restantes tras un cruce.
    """

    stable_side: int | None = None
    pending_side: int | None = None
    pending_count: int = 0
    cooldown_remaining: int = 0


@dataclass
class LineCounter:
    """Contador de cruces de una línea-umbral, por cámara.

    Mantiene una máquina de estado por ``track_id`` (semiplano estable, frames en
    el lado pendiente, banda muerta). ``process`` consume los tracks vivos de un
    frame y devuelve los ``CrossingEvent`` confirmados en ESE frame.

    Args:
        store: fuente del ``crossing_seq`` monótono (expone ``next_crossing_seq``).
        site_id/device_id/camera_id: identificadores (slugs validados).
        a, b: extremos normalizados de la línea (``Point`` o par ``(x, y)``).
        positive_side: +1/-1; qué flip cuenta como ``direction='in'``.
        positive_label/negative_label: etiquetas humanas del sentido.
        line_version: ``config_version`` de la línea en vigor (va a
            ``CrossingEvent.line_version``).
        min_frames: histéresis; frames consecutivos en el nuevo semiplano para
            confirmar un cruce (>=1; usa >=2 para anti-jitter real).
        cooldown: frames de banda muerta tras un cruce antes de admitir otro.
        schema_version: versión del contrato CrossingEvent (const 1).
    """

    store: _SeqSource
    site_id: str
    device_id: str
    camera_id: str
    a: XY
    b: XY
    positive_side: int
    positive_label: str | None = None
    negative_label: str | None = None
    line_version: int = 1
    min_frames: int = 2
    cooldown: int = 0
    schema_version: int = 1
    _states: dict[object, _TrackState] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        validate_site_id(self.site_id)
        validate_device_id(self.device_id)
        validate_camera_id(self.camera_id)
        if self.positive_side not in (-1, 1):
            raise ValueError(f"positive_side debe ser -1 o +1, no {self.positive_side!r}")
        if self.min_frames < 1:
            raise ValueError(f"min_frames debe ser >= 1, no {self.min_frames!r}")
        if self.cooldown < 0:
            raise ValueError(f"cooldown debe ser >= 0, no {self.cooldown!r}")
        self.a = _as_xy(self.a)
        self.b = _as_xy(self.b)

    @classmethod
    def from_config(cls, store: _SeqSource, config: LineConfig, **kwargs: object) -> LineCounter:
        """Construye un ``LineCounter`` desde un ``LineConfig`` (hot-reload).

        ``line_version`` se toma de ``config.config_version``. Los parámetros de
        robustez (``min_frames``, ``cooldown``) se pasan como kwargs.
        """
        return cls(
            store=store,
            site_id=config.site_id,
            device_id=config.device_id,
            camera_id=config.camera_id,
            a=_as_xy(config.line.a),
            b=_as_xy(config.line.b),
            positive_side=config.positive_side,
            positive_label=config.positive_label,
            negative_label=config.negative_label,
            line_version=config.config_version,
            schema_version=config.schema_version,
            **kwargs,  # type: ignore[arg-type]
        )

    def process(self, tracks: Iterable[_TrackLike], ts_event_ms: int) -> list[CrossingEvent]:
        """Procesa los tracks vivos de un frame y devuelve los cruces confirmados.

        Para cada track calcula su semiplano, avanza su máquina de histéresis y
        emite a lo sumo UN ``CrossingEvent`` por cruce confirmado. Los tracks que
        DESAPARECEN (no están en ``tracks``) se purgan: si un ``track_id`` vuelve a
        aparecer se trata como una trayectoria NUEVA (estado fresco), coherente
        con el ciclo de vida del tracker (retiro por ``max_age``).
        """
        events: list[CrossingEvent] = []
        present: set[object] = set()
        for track in tracks:
            tid = track.track_id
            present.add(tid)
            side = signed_side(self.a, self.b, _as_xy(track.centroid))
            state = self._states.get(tid)
            if state is None:
                state = _TrackState()
                self._states[tid] = state
            event = self._advance(state, tid, side, track, ts_event_ms)
            if event is not None:
                events.append(event)
        # Purga de tracks ausentes: su estado de histéresis ya no aplica.
        for tid in [t for t in self._states if t not in present]:
            del self._states[tid]
        return events

    def _advance(
        self,
        state: _TrackState,
        track_id: object,
        side: int,
        track: _TrackLike,
        ts_event_ms: int,
    ) -> CrossingEvent | None:
        """Avanza la máquina de histéresis de un track; devuelve el evento o ``None``."""
        # Banda muerta tras un cruce: no se confirma nada nuevo y se ignora el
        # jitter (no se acumulan frames pendientes).
        if state.cooldown_remaining > 0:
            state.cooldown_remaining -= 1
            state.pending_side = None
            state.pending_count = 0
            return None

        # Primera observación útil: fija el semiplano estable inicial.
        if state.stable_side is None:
            if side != 0:
                state.stable_side = side
            return None

        # Sobre la línea (side==0) o de vuelta al lado estable: no hay cruce; se
        # reinicia el candidato pendiente (esto es lo que mata el jitter).
        if side == 0 or side == state.stable_side:
            state.pending_side = None
            state.pending_count = 0
            return None

        # Candidato a flip hacia el semiplano contrario.
        if state.pending_side == side:
            state.pending_count += 1
        else:
            state.pending_side = side
            state.pending_count = 1

        if state.pending_count < self.min_frames:
            return None  # aún sin histéresis suficiente

        # Cruce CONFIRMADO: emite un único evento y adopta el nuevo semiplano.
        event = self._make_event(track_id, side, track, ts_event_ms)
        state.stable_side = side
        state.pending_side = None
        state.pending_count = 0
        state.cooldown_remaining = self.cooldown
        return event

    def _make_event(
        self, track_id: object, new_side: int, track: _TrackLike, ts_event_ms: int
    ) -> CrossingEvent:
        """Construye el ``CrossingEvent`` de un cruce confirmado (event_id determinista)."""
        track_id_str = str(track_id)
        crossing_seq = self.store.next_crossing_seq(self.camera_id)
        event_id = compute_event_id(
            self.site_id, self.device_id, self.camera_id, track_id_str, crossing_seq
        )
        direction = "in" if new_side == self.positive_side else "out"
        label = self.positive_label if direction == "in" else self.negative_label
        iso = ms_to_iso_utc(ts_event_ms)
        # El tracker de PR06 expone ``score``; el tipo Track de types usa ``confidence``.
        raw_conf = getattr(track, "score", None)
        if raw_conf is None:
            raw_conf = getattr(track, "confidence", 0.0)
        return CrossingEvent(
            event_id=event_id,
            site_id=self.site_id,
            device_id=self.device_id,
            camera_id=self.camera_id,
            track_id=track_id_str,
            crossing_seq=crossing_seq,
            direction=direction,
            ts_event_ms=int(ts_event_ms),
            ts_event_iso=iso,
            positive_label=self.positive_label,
            negative_label=self.negative_label,
            label=label,
            line_version=self.line_version,
            confidence=float(raw_conf or 0.0),
            clip_key=None,
            clip_status="pending",
            synced=0,
            created_at=iso,
            schema_version=self.schema_version,
        )
