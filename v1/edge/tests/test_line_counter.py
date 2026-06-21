"""Tests de la etapa ``count``: ``LineCounter`` (semiplano + histéresis).

Geometría sintética determinista en x86 sin hardware. Cubren el DoD:
- convención de SIGNO del producto cruzado -> ``direction`` (el contrato frágil),
- cruces en ambos sentidos (``'in'`` y ``'out'``),
- JITTER sobre la línea -> a lo sumo UN evento (histéresis ``min_frames``),
- un único evento por cruce (no re-emite mientras el track sigue al otro lado),
- ``event_id`` DETERMINISTA según la fórmula sha1,
- track RETIRADO que REAPARECE con el MISMO ``track_id`` -> DOS ``event_id``
  DISTINTOS (sin colisión) gracias al ``crossing_seq`` monótono por cámara,
- integración con el ``CentroidIoUTracker`` real de PR06.

La línea de referencia es VERTICAL: ``A=(0.5, 0.0)``, ``B=(0.5, 1.0)``. Con esa
orientación, el semiplano IZQUIERDO (x < 0.5) es ``+1`` y el DERECHO (x > 0.5)
es ``-1`` (ver ``test_signed_side_sign_convention``).
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass

from cam_counter_edge.line_counter import LineCounter, compute_event_id, signed_side
from cam_counter_edge.store import Store
from cam_counter_edge.tracker import CentroidIoUTracker
from cam_counter_edge.types import Detection

# Extremos de la línea-umbral vertical de referencia.
A = (0.5, 0.0)
B = (0.5, 1.0)


@dataclass
class _Track:
    """Track mínimo (duck-typing): ``track_id`` + ``centroid`` + ``score``."""

    track_id: object
    centroid: tuple[float, float]
    score: float = 0.9


class _FakeStore:
    """Fuente de ``crossing_seq`` en memoria (monótona por cámara)."""

    def __init__(self) -> None:
        self._seq: dict[str, int] = {}

    def next_crossing_seq(self, camera_id: str) -> int:
        self._seq[camera_id] = self._seq.get(camera_id, 0) + 1
        return self._seq[camera_id]


def _counter(store: object, *, positive_side: int = 1, min_frames: int = 2,
             cooldown: int = 0) -> LineCounter:
    return LineCounter(
        store=store,
        site_id="site-a",
        device_id="pi-001",
        camera_id="pi-001-cam0",
        a=A,
        b=B,
        positive_side=positive_side,
        positive_label="subieron",
        negative_label="bajaron",
        line_version=7,
        min_frames=min_frames,
        cooldown=cooldown,
    )


def _feed(lc: LineCounter, xs: list[float], *, track_id: object = 1, y: float = 0.5,
          t0: int = 1_700_000_000_000, dt: int = 100) -> list:
    """Alimenta una secuencia de posiciones x de UN track, frame a frame."""
    events: list = []
    for k, x in enumerate(xs):
        track = _Track(track_id=track_id, centroid=(x, y))
        events.extend(lc.process([track], ts_event_ms=t0 + k * dt))
    return events


# -- convención de signo (el contrato más frágil) -------------------------


def test_signed_side_sign_convention() -> None:
    """El signo del producto cruzado mapea a izquierda(+1)/derecha(-1) y 0 en la recta."""
    # Línea vertical A=(0.5,0)->B=(0.5,1): cross = (0.5 - px).
    assert signed_side(A, B, (0.2, 0.5)) == 1   # izquierda
    assert signed_side(A, B, (0.8, 0.5)) == -1  # derecha
    assert signed_side(A, B, (0.5, 0.5)) == 0   # exactamente sobre la línea
    assert signed_side(A, B, (0.5, 0.9)) == 0   # cualquier y sobre la recta
    # Invertir A y B invierte el signo (orientación de la línea).
    assert signed_side(B, A, (0.2, 0.5)) == -1


def test_sign_maps_to_expected_direction() -> None:
    """El semiplano de llegada (= ``positive_side``) determina ``direction='in'``."""
    store = _FakeStore()
    lc = _counter(store, positive_side=1, min_frames=2)
    # Cruza de la derecha (-1) a la izquierda (+1 == positive_side) -> 'in'.
    events = _feed(lc, [0.8, 0.2, 0.2])
    assert len(events) == 1
    assert events[0].direction == "in"
    assert signed_side(A, B, (0.2, 0.5)) == lc.positive_side


# -- cruces en ambos sentidos ---------------------------------------------


def test_cross_in_direction_and_label() -> None:
    """Cruce hacia ``positive_side`` -> ``'in'`` con ``label=positive_label``."""
    store = _FakeStore()
    lc = _counter(store, positive_side=1, min_frames=2)
    events = _feed(lc, [0.8, 0.2, 0.2])
    assert len(events) == 1
    ev = events[0]
    assert ev.direction == "in"
    assert ev.label == "subieron"
    assert ev.line_version == 7
    assert ev.clip_status == "pending" and ev.clip_key is None
    assert ev.confidence == 0.9


def test_cross_out_direction_and_label() -> None:
    """Cruce hacia el semiplano contrario -> ``'out'`` con ``label=negative_label``."""
    store = _FakeStore()
    lc = _counter(store, positive_side=1, min_frames=2)
    events = _feed(lc, [0.2, 0.8, 0.8])
    assert len(events) == 1
    assert events[0].direction == "out"
    assert events[0].label == "bajaron"


def test_positive_side_inverts_direction() -> None:
    """Cambiar ``positive_side`` invierte qué flip es ``'in'``."""
    store = _FakeStore()
    lc = _counter(store, positive_side=-1, min_frames=2)
    # Mismo movimiento derecha->izquierda, pero ahora +1 NO es positive_side.
    events = _feed(lc, [0.8, 0.2, 0.2])
    assert len(events) == 1
    assert events[0].direction == "out"


# -- histéresis / jitter ---------------------------------------------------


def test_jitter_over_line_counts_at_most_once() -> None:
    """Un centroide que tiembla SOBRE la línea no produce doble conteo."""
    # Pura oscilación (sin asentarse) -> 0 eventos: nunca acumula min_frames.
    osc = _counter(_FakeStore(), positive_side=1, min_frames=2)
    only_jitter = _feed(osc, [0.8, 0.45, 0.55, 0.45, 0.55, 0.45, 0.55])
    assert len(only_jitter) == 0

    # Oscila y AL FINAL se asienta en el nuevo lado -> EXACTAMENTE 1 evento.
    settle = _counter(_FakeStore(), positive_side=1, min_frames=2)
    events = _feed(settle, [0.8, 0.45, 0.55, 0.45, 0.55, 0.45, 0.45])
    assert len(events) == 1
    assert events[0].direction == "in"


def test_one_event_per_crossing_no_reemit() -> None:
    """Tras confirmar un cruce no se re-emite mientras el track sigue al otro lado."""
    store = _FakeStore()
    lc = _counter(store, positive_side=1, min_frames=2)
    # Cruza una vez y se queda a la izquierda muchos frames: un único evento.
    events = _feed(lc, [0.8, 0.2, 0.2, 0.2, 0.2, 0.2, 0.2])
    assert len(events) == 1


def test_cooldown_suppresses_immediate_rebounce() -> None:
    """Con ``cooldown`` un rebote inmediato tras el cruce no cuenta otra vez."""
    store = _FakeStore()
    lc = _counter(store, positive_side=1, min_frames=1, cooldown=3)
    # Cruza (0.2) y rebota de inmediato (0.8,0.8) dentro del cooldown -> 1 evento.
    events = _feed(lc, [0.8, 0.2, 0.8, 0.8])
    assert len(events) == 1
    assert events[0].direction == "in"


# -- event_id determinista -------------------------------------------------


def test_event_id_is_deterministic() -> None:
    """``event_id`` = sha1('site|device|camera|track|seq') en hex minúscula."""
    store = _FakeStore()
    lc = _counter(store, positive_side=1, min_frames=1)
    events = _feed(lc, [0.8, 0.2], track_id=42)
    assert len(events) == 1
    ev = events[0]
    expected_raw = f"site-a|pi-001|pi-001-cam0|42|{ev.crossing_seq}"
    expected = hashlib.sha1(expected_raw.encode("utf-8")).hexdigest()
    assert ev.event_id == expected
    assert ev.event_id == compute_event_id("site-a", "pi-001", "pi-001-cam0", "42", ev.crossing_seq)
    assert len(ev.event_id) == 40 and ev.event_id == ev.event_id.lower()
    assert ev.track_id == "42"  # se almacena como string


# -- reaparición de track / colisión de crossing_seq evitada ---------------


def test_reappear_seq_collision_distinct_event_ids(tmp_path) -> None:
    """Track retirado que REAPARECE con el MISMO ``track_id`` -> 2 ``event_id`` distintos.

    Aunque ambos cruces compartan ``track_id``, el ``crossing_seq`` monótono por
    cámara (persistido en ``store``) les da ``event_id`` DISTINTOS: no hay colisión
    y la sync idempotente no descarta el segundo cruce como duplicado.
    """
    store = Store(str(tmp_path / "events.db"))
    lc = _counter(store, positive_side=1, min_frames=1)

    # Fase 1: track 7 cruza una vez.
    first = _feed(lc, [0.8, 0.2], track_id=7, t0=1_700_000_000_000)
    assert len(first) == 1

    # El track 7 se RETIRA (frame sin tracks): su estado de histéresis se purga.
    assert lc.process([], ts_event_ms=1_700_000_000_500) == []

    # Fase 2: un track NUEVO reusa el id 7 y vuelve a cruzar.
    second = _feed(lc, [0.8, 0.2], track_id=7, t0=1_700_000_001_000)
    assert len(second) == 1

    e1, e2 = first[0], second[0]
    assert e1.track_id == e2.track_id == "7"            # mismo track_id
    assert e1.crossing_seq != e2.crossing_seq           # crossing_seq distinto
    assert (e1.crossing_seq, e2.crossing_seq) == (1, 2)
    assert e1.event_id != e2.event_id                   # SIN colisión de event_id
    # Persistencia idempotente: ambos eventos coexisten (no se deduplican entre sí).
    assert store.insert_event(e1) is True
    assert store.insert_event(e2) is True
    assert len(store.get_recent_events("pi-001-cam0")) == 2
    store.close()


# -- integración con el tracker real de PR06 -------------------------------


def _det(cx: float, cy: float, w: float = 0.3, h: float = 0.4, score: float = 0.9) -> Detection:
    return Detection(bbox_norm=[cx - w / 2, cy - h / 2, cx + w / 2, cy + h / 2], confidence=score)


def test_integration_with_real_tracker_emits_single_in_event(tmp_path) -> None:
    """El ``CentroidIoUTracker`` real alimenta al ``LineCounter`` y produce 1 'in'."""
    store = Store(str(tmp_path / "events.db"))
    lc = _counter(store, positive_side=1, min_frames=2)
    tracker = CentroidIoUTracker(iou_threshold=0.3, max_age=30)

    events: list = []
    # Una persona cruza despacio de derecha (x>0.5) a izquierda (x<0.5).
    for k, cx in enumerate([0.72, 0.67, 0.62, 0.57, 0.52, 0.47, 0.42, 0.37]):
        active = tracker.update([_det(cx, 0.5)], ts=float(k))
        events.extend(lc.process(active, ts_event_ms=1_700_000_000_000 + k * 100))

    assert len(events) == 1
    ev = events[0]
    assert ev.direction == "in"
    # El track_id del evento es el id (como string) que asignó el tracker real.
    assert ev.track_id == str(tracker.tracks[0].track_id)
    assert store.record_event(ev) is True
    store.close()
