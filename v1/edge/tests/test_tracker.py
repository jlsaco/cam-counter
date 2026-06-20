"""Contrato de CentroidIoUTracker: ids estables, sin reutilización, deterministas.

Secuencias sintéticas y deterministas (numpy puro, sin hardware): cajas de ancho 0.2 con
desplazamientos de 0.05 -> el IoU entre frames consecutivos (~0.6) supera el umbral por
defecto (0.3), de modo que la asociación es inequívoca y testeable.
"""

from __future__ import annotations

from cam_counter_edge.tracker import CentroidIoUTracker, Track, Tracker
from cam_counter_edge.types import Detection


def _det(xmin: float, ymin: float, xmax: float, ymax: float, score: float = 0.9) -> Detection:
    """Detección de persona con caja normalizada ``[xmin, ymin, xmax, ymax]``."""
    return Detection(bbox_norm=[xmin, ymin, xmax, ymax], class_id=0, confidence=score)


def _track_by_lane(tracks: list[Track], cy: float, tol: float = 0.05) -> Track:
    """Devuelve el único track cuyo centroide-y cae cerca de ``cy`` (carril)."""
    near = [t for t in tracks if abs(t.centroid[1] - cy) <= tol]
    assert len(near) == 1, f"esperaba 1 track en el carril y={cy}, hay {len(near)}"
    return near[0]


def test_centroid_iou_tracker_is_a_tracker():
    """CentroidIoUTracker implementa la interfaz base abstracta Tracker."""
    assert issubclass(CentroidIoUTracker, Tracker)
    assert isinstance(CentroidIoUTracker(), Tracker)


def test_single_person_keeps_stable_track_id():
    """Una persona moviéndose por varios frames conserva el MISMO track_id."""
    tracker = CentroidIoUTracker(iou_threshold=0.3, max_age=30, max_history=30)
    ids = []
    for i in range(8):
        x = 0.10 + 0.05 * i
        tracks = tracker.update([_det(x, 0.20, x + 0.20, 0.40)], ts=float(i))
        assert len(tracks) == 1
        ids.append(tracks[0].track_id)
    # Mismo id en todos los frames y track maduro (hits crecientes, sin frames perdidos).
    assert len(set(ids)) == 1
    assert tracker.tracks[0].hits == 8
    assert tracker.tracks[0].time_since_update == 0
    assert tracker.tracks[0].age == 7  # 8 frames: edad = frames desde la creación
    # El historial está acotado por max_history y guarda los centroides observados.
    assert len(tracker.tracks[0].history) == 8


def test_two_crossing_paths_preserve_track_ids():
    """Dos pistas cuyas trayectorias se cruzan (en x) NO intercambian su track_id.

    Carriles separados en y (0.2 vs 0.7): el IoU cruzado es 0, así la asociación por IoU
    mantiene cada identidad pese a que sus posiciones-x se cruzan a mitad de la secuencia.
    """
    tracker = CentroidIoUTracker(iou_threshold=0.3, max_age=30)
    # Frame 0 (creación): A (carril superior) primero -> id menor; B (inferior) después.
    a0x, b0x = 0.10, 0.30
    tracks = tracker.update(
        [_det(a0x, 0.10, a0x + 0.20, 0.30), _det(b0x, 0.60, b0x + 0.20, 0.80)], ts=0.0
    )
    assert len(tracks) == 2
    id_top = _track_by_lane(tracks, cy=0.20).track_id
    id_bot = _track_by_lane(tracks, cy=0.70).track_id
    assert id_top != id_bot

    # Frames 1..4: A va a la derecha, B a la izquierda; se cruzan en x (en x=0.2 en i=2).
    for i in range(1, 5):
        ax = 0.10 + 0.05 * i
        bx = 0.30 - 0.05 * i
        tracks = tracker.update(
            [_det(ax, 0.10, ax + 0.20, 0.30), _det(bx, 0.60, bx + 0.20, 0.80)], ts=float(i)
        )
        assert len(tracks) == 2
        # Cada carril conserva EXACTAMENTE su id original: no hay swap de identidades.
        assert _track_by_lane(tracks, cy=0.20).track_id == id_top
        assert _track_by_lane(tracks, cy=0.70).track_id == id_bot


def test_retire_after_max_age_removes_track():
    """Tras max_age frames consecutivos sin match, el track ya no aparece en los activos."""
    max_age = 3
    tracker = CentroidIoUTracker(iou_threshold=0.3, max_age=max_age)
    # Un par de frames con detección: el track existe y madura.
    tracker.update([_det(0.40, 0.40, 0.60, 0.60)], ts=0.0)
    tracks = tracker.update([_det(0.41, 0.40, 0.61, 0.60)], ts=1.0)
    assert len(tracks) == 1

    # Frames vacíos: aguanta hasta max_age-1 ausencias y desaparece en la ausencia max_age.
    for miss in range(1, max_age):
        tracks = tracker.update([], ts=float(1 + miss))
        assert len(tracks) == 1, f"a {miss} frames sin match el track debe seguir activo"
        assert tracks[0].time_since_update == miss
    tracks = tracker.update([], ts=float(1 + max_age))
    assert tracks == [], "tras max_age frames sin match el track debe estar retirado"
    assert tracker.tracks == []


def test_reappear_after_retire_gets_new_id_no_reuse():
    """Una pista que reaparece tras ser retirada recibe un id NUEVO (jamás el retirado)."""
    max_age = 3
    tracker = CentroidIoUTracker(iou_threshold=0.3, max_age=max_age)
    # Pista presente y luego retirada.
    first = tracker.update([_det(0.40, 0.40, 0.60, 0.60)], ts=0.0)
    retired_id = first[0].track_id
    # Ausencia prolongada (MÁS de max_age frames) para garantizar el retiro.
    for k in range(max_age + 3):
        tracker.update([], ts=float(1 + k))
    assert tracker.tracks == []

    # Reaparece en (aprox.) la misma posición: debe ser un track NUEVO, no el retirado.
    reappeared = tracker.update([_det(0.40, 0.40, 0.60, 0.60)], ts=100.0)
    assert len(reappeared) == 1
    new_id = reappeared[0].track_id
    assert new_id != retired_id, "el id retirado NO debe reutilizarse"
    assert new_id > retired_id, "el asignador de ids es monótono (sólo incrementa)"


def test_far_detection_creates_new_id_without_reuse():
    """Una detección sin solape (IoU bajo el umbral) crea un track nuevo, sin reutilizar."""
    tracker = CentroidIoUTracker(iou_threshold=0.3, max_age=30)
    a = tracker.update([_det(0.05, 0.05, 0.20, 0.20)], ts=0.0)
    id_a = a[0].track_id
    # Detección lejana (sin solape) + la original: la lejana es un id nuevo, distinto.
    out = tracker.update(
        [_det(0.05, 0.05, 0.20, 0.20), _det(0.75, 0.75, 0.95, 0.95)], ts=1.0
    )
    ids = sorted(t.track_id for t in out)
    assert len(ids) == 2
    assert id_a in ids
    assert ids[-1] > id_a  # el nuevo id es mayor (monótono), nunca uno reutilizado


def test_deterministic_same_input_same_ids():
    """La misma secuencia produce exactamente la misma salida e ids en dos instancias."""
    frames = [
        [_det(0.10, 0.20, 0.30, 0.40)],
        [_det(0.15, 0.20, 0.35, 0.40), _det(0.60, 0.60, 0.80, 0.80)],
        [],
        [_det(0.20, 0.20, 0.40, 0.40)],
        [_det(0.62, 0.60, 0.82, 0.80)],
    ]

    def run() -> list[list[tuple[int, tuple[float, float, float, float]]]]:
        tracker = CentroidIoUTracker(iou_threshold=0.3, max_age=2)
        seq = []
        for i, frame in enumerate(frames):
            tracks = tracker.update(frame, ts=float(i))
            seq.append(
                sorted((t.track_id, tuple(t.bbox_norm)) for t in tracks)
            )
        return seq

    assert run() == run()
