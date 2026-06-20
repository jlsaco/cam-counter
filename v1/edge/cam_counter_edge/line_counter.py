"""Etapa ``count`` del pipeline de borde: conteo de cruce de la línea-umbral.

Implementa el ``LineCounter`` por cámara. Recibe los tracks vivos del tracker (PR06) en cada
frame y detecta el **cruce de una línea-umbral** definida por dos extremos normalizados
``A``,``B`` (0..1). El criterio es de **semiplano por signo del producto cruzado** con
**histéresis** (banda muerta anti-jitter) e **idempotencia por track** (un mismo cruce no se
recuenta).

Contrato de signo (el más frágil del sistema; ver CLAUDE.md):

    side(P) = sign(cross(B - A, P - A)),   cross(u, v) = u.x*v.y - u.y*v.x

Un **cruce** ocurre cuando el signo de ``side`` cambia (flip) entre el centroide previo y el
actual del MISMO track. ``positive_side`` (+1/-1) selecciona qué semiplano cuenta como
``'in'``: si el track acaba en el semiplano ``positive_side`` el cruce es ``'in'``, si acaba
en el contrario es ``'out'``. ``'subieron'``/``'bajaron'`` son ``positive_label`` /
``negative_label`` (etiquetas humanas de presentación); el valor de cable almacenado es
EXACTAMENTE ``'in'`` o ``'out'``.

Idempotencia: cada cruce confirmado pide a ``store.next_crossing_seq(camera_id)`` un
``crossing_seq`` MONÓTONO POR CÁMARA y deriva ``event_id`` determinista. Así, aunque el
tracker reuse un ``track_id`` (tras retirar un track y aparecer otro), dos cruces distintos
NUNCA colisionan en ``event_id``.

El ``LineCounter`` NO escribe los eventos en la DB: su único efecto sobre el store es pedir
el ``crossing_seq``. La persistencia (``insert_event`` + ``bump_counter``, idempotente) la
decide el caller, p.ej. vía :meth:`Store.record_event`. Todo es stdlib pura (``hashlib``,
``math``, ``datetime``); corre en CI x86 sin hardware con geometría sintética determinista.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from .identifiers import validate_camera_id, validate_device_id, validate_site_id
from .types import SCHEMA_VERSION, CrossingEvent, LineConfig, Point

# Defaults de robustez. ``min_frames`` = nº de frames consecutivos que el centroide debe
# permanecer en el NUEVO semiplano para CONFIRMAR el cruce (histéresis anti-jitter).
# ``cooldown`` = frames tras un cruce confirmado durante los que no se cuenta otro cruce
# (banda muerta temporal extra; el cruce inverso genuino vuelve a contar al expirar).
DEFAULT_MIN_FRAMES = 2
DEFAULT_COOLDOWN = 0


def cross(u: Point, v: Point) -> float:
    """Producto cruzado 2D ``u.x*v.y - u.y*v.x`` (componente z del cruce 3D)."""
    return u[0] * v[1] - u[1] * v[0]


def signed_side(a: Point, b: Point, p: Point) -> int:
    """Signo del semiplano de ``p`` respecto de la línea dirigida ``A->B``.

    Devuelve ``+1`` / ``-1`` según el lado, o ``0`` si ``p`` cae EXACTAMENTE sobre la recta.
    Es ``sign(cross(B - A, P - A))`` (contrato de signo del sistema). Función PURA y
    aislada para poder asertar la convención de signo directamente en los tests.
    """
    ux, uy = (b[0] - a[0], b[1] - a[1])
    vx, vy = (p[0] - a[0], p[1] - a[1])
    c = ux * vy - uy * vx
    if c > 0.0:
        return 1
    if c < 0.0:
        return -1
    return 0


def make_event_id(
    site_id: str, device_id: str, camera_id: str, track_id: str, crossing_seq: int
) -> str:
    """Construye el ``event_id`` DETERMINISTA del contrato CrossingEvent.

    ``event_id = sha1('{site_id}|{device_id}|{camera_id}|{track_id}|{crossing_seq}')`` en hex
    minúscula. El sha1 aquí es **NO CRIPTOGRÁFICO**: se usa SÓLO como huella de deduplicación
    idempotente del sync (un reintento del mismo ``event_id`` no duplica), nunca para firmar
    ni proteger nada. Por eso ``usedforsecurity=False`` y no es un hallazgo de seguridad.
    """
    payload = f"{site_id}|{device_id}|{camera_id}|{track_id}|{crossing_seq}"
    # sha1 NO criptográfico (dedupe only): hint explícito para gitleaks/security-review.
    digest = hashlib.sha1(payload.encode("utf-8"), usedforsecurity=False)
    return digest.hexdigest()


def iso_from_ms(ts_event_ms: int) -> str:
    """Convierte epoch ms UTC a ISO-8601 UTC determinista (sufijo ``Z``).

    Derivado SÓLO de ``ts_event_ms`` (no del reloj real), de modo que los tests son
    deterministas. Ejemplo: ``1700000000000 -> '2023-11-14T22:13:20+00:00'`` con ``Z``.
    """
    dt = datetime.fromtimestamp(ts_event_ms / 1000.0, tz=timezone.utc)
    return dt.isoformat().replace("+00:00", "Z")


@dataclass
class _TrackCrossState:
    """Estado de la máquina de cruce de UN track (interno al LineCounter).

    Attributes:
        committed_side: semiplano (-1/+1) en el que el track está "asentado"; 0 = aún sin
            asentar (no se ha visto un lado no-nulo).
        cand_side: semiplano candidato que se está acumulando para confirmar el flip.
        cand_count: nº de frames consecutivos del centroide en ``cand_side``.
        cooldown_left: frames restantes de banda muerta tras un cruce confirmado.
    """

    committed_side: int = 0
    cand_side: int = 0
    cand_count: int = 0
    cooldown_left: int = 0


@dataclass
class LineCounter:
    """Contador de cruce de línea por cámara (semiplano + histéresis + idempotencia).

    Una instancia por cámara. Mantiene una máquina de estado por ``track_id`` (semiplano
    asentado, candidato en acumulación, cooldown) y emite EXACTAMENTE UN ``CrossingEvent``
    por cruce genuino confirmado.
    """

    site_id: str
    device_id: str
    camera_id: str
    a: Point
    b: Point
    store: Any
    positive_side: int = 1
    positive_label: str = "in"
    negative_label: str = "out"
    line_version: int = 1
    min_frames: int = DEFAULT_MIN_FRAMES
    cooldown: int = DEFAULT_COOLDOWN
    _states: dict[str, _TrackCrossState] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        # Validar slugs ANTES de poder construir cualquier event_id/clave (CLAUDE.md §3).
        validate_site_id(self.site_id)
        validate_device_id(self.device_id)
        validate_camera_id(self.camera_id)
        if self.positive_side not in (-1, 1):
            raise ValueError(f"positive_side inválido: {self.positive_side!r} (se espera +1/-1)")
        if int(self.min_frames) < 1:
            raise ValueError(f"min_frames debe ser >= 1, no {self.min_frames!r}")
        self.min_frames = int(self.min_frames)
        self.cooldown = int(self.cooldown)

    @classmethod
    def from_config(
        cls,
        config: LineConfig,
        store: Any,
        *,
        min_frames: int = DEFAULT_MIN_FRAMES,
        cooldown: int = DEFAULT_COOLDOWN,
    ) -> LineCounter:
        """Crea un ``LineCounter`` a partir de un ``LineConfig`` (espejo del contrato)."""
        return cls(
            site_id=config.site_id,
            device_id=config.device_id,
            camera_id=config.camera_id,
            a=tuple(config.a),
            b=tuple(config.b),
            store=store,
            positive_side=config.positive_side,
            positive_label=config.positive_label,
            negative_label=config.negative_label,
            line_version=config.config_version,
            min_frames=min_frames,
            cooldown=cooldown,
        )

    # ────────────────────────────────── geometría ──────────────────────────────
    def side_of(self, point: Point) -> int:
        """Semiplano (-1/0/+1) de ``point`` respecto de la línea ``A->B`` de esta cámara."""
        return signed_side(self.a, self.b, point)

    def _direction_for_side(self, new_side: int) -> str:
        """Mapea el semiplano FINAL de un cruce a la dirección de cable ``'in'``/``'out'``."""
        return "in" if new_side == self.positive_side else "out"

    # ─────────────────────────────────── proceso ───────────────────────────────
    def process(self, tracks: Any, ts_event_ms: int) -> list[CrossingEvent]:
        """Procesa los tracks vivos de un frame y devuelve los cruces confirmados.

        Args:
            tracks: iterable de tracks vivos del frame. Cada track debe exponer
                ``track_id`` y ``centroid`` ``(x, y)`` normalizado (forma del tracker de
                PR06); ``confidence``/``score`` es opcional.
            ts_event_ms: epoch ms UTC AUTORITATIVO del frame; se pasa explícito para que el
                conteo sea determinista (no depende del reloj real). ``ts_event_iso`` se
                deriva de él.

        Returns:
            Lista de ``CrossingEvent`` confirmados en este frame (vacía si no hubo cruces).
            La persistencia la decide el caller (p.ej. ``store.record_event``); el único
            efecto de este método sobre el store es ``next_crossing_seq`` por cada cruce.
        """
        track_list = list(tracks)
        seen_ids: set[str] = set()
        events: list[CrossingEvent] = []

        for track in track_list:
            track_id = str(track.track_id)
            seen_ids.add(track_id)
            centroid = _centroid_of(track)
            event = self._advance_track(track_id, centroid, track, ts_event_ms)
            if event is not None:
                events.append(event)

        # Poda: el tracker es la autoridad de liveness y NUNCA reusa un id dentro de su
        # sesión, así que un track ausente del frame está RETIRADO -> se olvida su estado.
        # Si más tarde reaparece otro track con un id reusado, arranca limpio (committed=0).
        stale = [tid for tid in self._states if tid not in seen_ids]
        for tid in stale:
            del self._states[tid]

        return events

    def _advance_track(
        self, track_id: str, centroid: Point, track: Any, ts_event_ms: int
    ) -> CrossingEvent | None:
        """Avanza la máquina de estado de un track y, si confirma un cruce, lo emite."""
        state = self._states.get(track_id)
        if state is None:
            state = _TrackCrossState()
            self._states[track_id] = state

        side = self.side_of(centroid)

        # Primera observación con lado definido: el track se asienta sin contar cruce.
        if state.committed_side == 0:
            if side != 0:
                state.committed_side = side
            return None

        # Banda muerta tras un cruce: consume cooldown sin acumular candidatos.
        if state.cooldown_left > 0:
            state.cooldown_left -= 1
            state.cand_side = 0
            state.cand_count = 0
            return None

        # Centroide sobre la línea (0) o de vuelta en el lado asentado: reset del candidato.
        if side == 0 or side == state.committed_side:
            state.cand_side = 0
            state.cand_count = 0
            return None

        # Centroide en el semiplano OPUESTO: acumula frames de histéresis.
        if side == state.cand_side:
            state.cand_count += 1
        else:
            state.cand_side = side
            state.cand_count = 1

        if state.cand_count < self.min_frames:
            return None  # aún dentro de la banda muerta de histéresis: no se confirma.

        # ── Cruce CONFIRMADO ──
        new_side = side
        state.committed_side = new_side
        state.cand_side = 0
        state.cand_count = 0
        state.cooldown_left = self.cooldown
        return self._emit(track_id, new_side, track, ts_event_ms)

    def _emit(
        self, track_id: str, new_side: int, track: Any, ts_event_ms: int
    ) -> CrossingEvent:
        """Construye el ``CrossingEvent`` determinista de un cruce confirmado."""
        crossing_seq = int(self.store.next_crossing_seq(self.camera_id))
        event_id = make_event_id(
            self.site_id, self.device_id, self.camera_id, track_id, crossing_seq
        )
        direction = self._direction_for_side(new_side)
        label = self.positive_label if direction == "in" else self.negative_label
        ts_iso = iso_from_ms(ts_event_ms)
        return CrossingEvent(
            event_id=event_id,
            site_id=self.site_id,
            device_id=self.device_id,
            camera_id=self.camera_id,
            track_id=track_id,
            crossing_seq=crossing_seq,
            direction=direction,
            label=label,
            line_version=self.line_version,
            ts_event_ms=int(ts_event_ms),
            ts_event_iso=ts_iso,
            confidence=_confidence_of(track),
            clip_key=None,
            clip_status="pending",
            schema_version=SCHEMA_VERSION,
            synced=0,
            created_at=ts_iso,
            positive_label=self.positive_label,
            negative_label=self.negative_label,
        )

    def forget_track(self, track_id: str) -> None:
        """Olvida el estado de un track (p.ej. al retirarse). Idempotente."""
        self._states.pop(str(track_id), None)

    def reset(self) -> None:
        """Reinicia toda la máquina de estado (no toca el ``crossing_seq`` del store)."""
        self._states.clear()


def _centroid_of(track: Any) -> Point:
    """Extrae el centroide ``(x, y)`` de un track del tracker (o de un dict/tupla)."""
    centroid = getattr(track, "centroid", None)
    if centroid is None and isinstance(track, dict):
        centroid = track.get("centroid")
    if centroid is None:
        raise AttributeError(f"track sin 'centroid': {track!r}")
    cx, cy = centroid
    return (float(cx), float(cy))


def _confidence_of(track: Any) -> float:
    """Extrae la confianza de un track: ``confidence`` (types.Track) o ``score`` (tracker)."""
    value = getattr(track, "confidence", None)
    if value is None:
        value = getattr(track, "score", None)
    if value is None and isinstance(track, dict):
        value = track.get("confidence", track.get("score"))
    return float(value) if value is not None else 0.0


__all__ = [
    "LineCounter",
    "signed_side",
    "cross",
    "make_event_id",
    "iso_from_ms",
    "DEFAULT_MIN_FRAMES",
    "DEFAULT_COOLDOWN",
]
