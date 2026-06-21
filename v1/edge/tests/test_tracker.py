"""Tests de la etapa ``track``: ``CentroidIoUTracker``.

Cubren el contrato crítico del tracker en x86 sin hardware:
- estabilidad del ``track_id`` de una pista a lo largo de frames,
- conservación de ids cuando dos trayectorias se cruzan (no se intercambian),
- asignación de un id NUEVO al reaparecer (los ids retirados NO se reutilizan),
- retiro de una pista tras ``max_age`` frames sin match,
- determinismo (misma secuencia -> mismos ids).

Toda la geometría va en floats normalizados 0..1, orden ``[xmin,ymin,xmax,ymax]``.
"""

from __future__ import annotations

from cam_counter_edge.tracker import CentroidIoUTracker, Track, Tracker
from cam_counter_edge.types import Detection


def _det(cx: float, cy: float, w: float = 0.12, h: float = 0.20, score: float = 0.9) -> Detection:
    """Detección centrada en ``(cx, cy)`` con tamaño ``w x h`` (normalizado)."""
    return Detection(
        bbox_norm=[cx - w / 2.0, cy - h / 2.0, cx + w / 2.0, cy + h / 2.0],
        confidence=score,
    )


def test_tracker_is_abstract_base() -> None:
    """``Tracker`` es abstracto y ``CentroidIoUTracker`` lo implementa."""
    assert issubclass(CentroidIoUTracker, Tracker)


def test_single_track_keeps_stable_id() -> None:
    """Una persona moviéndose despacio conserva el MISMO ``track_id``."""
    tr = CentroidIoUTracker(iou_threshold=0.3, max_age=30)
    ids: set[int] = set()
    track: Track | None = None
    for k, cx in enumerate([0.20, 0.23, 0.26, 0.29, 0.32]):
        active = tr.update([_det(cx, 0.50)], ts=float(k))
        assert len(active) == 1
        track = active[0]
        ids.add(track.track_id)
    # Un único id a lo largo de toda la secuencia; el historial fue creciendo.
    assert len(ids) == 1
    assert track is not None
    assert track.hits == 5
    assert track.time_since_update == 0
    assert track.age == 4
    assert len(track.history) == 5


def test_two_crossing_tracks_do_not_swap_ids() -> None:
    """Dos trayectorias que se cruzan en x conservan cada una su ``track_id``.

    A avanza de izquierda a derecha por ``y=0.30``; B de derecha a izquierda por
    ``y=0.70``. Sus x se cruzan a mitad de recorrido, pero como están en bandas y
    distintas el IoU empareja siempre cada detección con su pista correcta.
    """
    tr = CentroidIoUTracker(iou_threshold=0.3, max_age=30)
    id_low: int | None = None  # pista de la banda superior (y ~ 0.30)
    id_high: int | None = None  # pista de la banda inferior (y ~ 0.70)

    for k in range(9):
        a_x = 0.20 + 0.05 * k  # 0.20 -> 0.60
        b_x = 0.80 - 0.05 * k  # 0.80 -> 0.40 (cruzan en x=0.50 en k=6)
        active = tr.update([_det(a_x, 0.30), _det(b_x, 0.70)], ts=float(k))
        assert len(active) == 2

        # Identifica cada pista por su banda en y (no por el orden de la lista).
        low = next(t for t in active if t.centroid[1] < 0.5)
        high = next(t for t in active if t.centroid[1] >= 0.5)

        if id_low is None:
            id_low, id_high = low.track_id, high.track_id
            assert id_low != id_high
        else:
            # Los ids NO se intercambian al cruzarse las trayectorias.
            assert low.track_id == id_low
            assert high.track_id == id_high


def test_track_is_retired_after_max_age() -> None:
    """Tras ``max_age`` frames consecutivos sin match, la pista se retira."""
    max_age = 3
    tr = CentroidIoUTracker(iou_threshold=0.3, max_age=max_age)

    active = tr.update([_det(0.30, 0.50)], ts=0.0)
    assert len(active) == 1
    original_id = active[0].track_id

    # Frames vacíos: la pista sobrevive hasta justo antes de max_age...
    for k in range(1, max_age):
        active = tr.update([], ts=float(k))
        assert len(active) == 1
        assert active[0].track_id == original_id
        assert active[0].time_since_update == k

    # ...y en el frame nº max_age sin match desaparece de las activas.
    active = tr.update([], ts=float(max_age))
    assert active == []
    assert all(t.track_id != original_id for t in tr.tracks)


def test_reappearing_track_gets_new_id_no_reuse() -> None:
    """Una pista que reaparece tras retirarse recibe un id NUEVO (sin reutilizar).

    Protege el determinismo del ``event_id`` aguas abajo: un id retirado NUNCA se
    reasigna dentro de la misma sesión de la cámara.
    """
    max_age = 2
    tr = CentroidIoUTracker(iou_threshold=0.3, max_age=max_age)

    # 1) Aparece una persona.
    active = tr.update([_det(0.30, 0.50)], ts=0.0)
    retired_id = active[0].track_id

    # 2) Desaparece durante (más de) max_age frames hasta retirarse.
    for k in range(1, max_age + 2):
        active = tr.update([], ts=float(k))
    assert active == []  # ya no quedan pistas activas

    # 3) Reaparece una detección en (casi) la misma posición.
    active = tr.update([_det(0.31, 0.50)], ts=99.0)
    assert len(active) == 1
    new_id = active[0].track_id

    # El id es NUEVO: ni igual al retirado ni anterior (contador monótono).
    assert new_id != retired_id
    assert new_id > retired_id


def test_ids_are_monotonic_and_unique_per_new_detection() -> None:
    """Cada detección nueva simultánea recibe un id fresco y creciente."""
    tr = CentroidIoUTracker(iou_threshold=0.3, max_age=30)
    active = tr.update([_det(0.20, 0.30), _det(0.80, 0.70)], ts=0.0)
    ids = sorted(t.track_id for t in active)
    assert len(set(ids)) == 2
    assert ids == [1, 2]  # contador monótono empieza en 1


def test_deterministic_same_ids_across_runs() -> None:
    """La misma secuencia de entradas produce exactamente los mismos ids."""

    def run() -> list[tuple[int, ...]]:
        tr = CentroidIoUTracker(iou_threshold=0.3, max_age=5)
        out: list[tuple[int, ...]] = []
        frames = [
            [_det(0.20, 0.30), _det(0.80, 0.70)],
            [_det(0.25, 0.30), _det(0.75, 0.70)],
            [_det(0.30, 0.30)],  # B desaparece
            [],
            [_det(0.35, 0.30), _det(0.50, 0.50)],  # nuevo objeto en el centro
        ]
        for k, frame in enumerate(frames):
            active = tr.update(frame, ts=float(k))
            out.append(tuple(sorted(t.track_id for t in active)))
        return out

    assert run() == run()
